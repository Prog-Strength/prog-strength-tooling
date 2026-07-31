"""Logging setup and timing helpers for the CLI.

Every command writes progress and diagnostics through the standard `logging`
module to **stderr**, so `--json` on stdout stays pipeable. The default level is
INFO — phase boundaries and durations, enough to answer "what is it doing right
now?" — and `-v` / `-vv` open it up to DEBUG.

The split that keeps default output readable: INFO is phase boundaries and
durations; anything a table already renders stays in `render.py`.

This module imports nothing else from the package (it deliberately does not
import `render`, which pulls in `whoop`, `health`, and `models`), so any module
can log without creating an import cycle.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

#: Logger name every module in this package logs under, via `get_logger`.
ROOT_LOGGER = "prog_strength_tooling"

#: Env var an operator can set instead of passing -v/-q on every command.
ENV_LOG_LEVEL = "PST_LOG_LEVEL"

#: Third-party loggers that only -vv turns on. These are the wire logs — every
#: signed AWS request, every httpx connection event. Invaluable when the
#: question is "did the request even leave the machine?", overwhelming
#: otherwise, which is why they need a level of their own rather than riding
#: along with our own DEBUG.
WIRE_LOGGERS = ("boto3", "botocore", "urllib3", "httpx", "httpcore")

#: Level when nothing selects one.
DEFAULT_LEVEL = logging.INFO

#: The single source of truth for parsing PST_LOG_LEVEL. Deliberately not
#: consulted once a flag (--quiet, -v, -vv) has already decided the level —
#: see resolve_level's precedence order below.
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def resolve_level(
    verbosity: int, quiet: bool, env_value: str | None
) -> tuple[int, bool, str | None]:
    """Resolve (level, wire_logs_on, warning) from flags, then env, then default.

    Mirrors the flag -> env -> default precedence `config.py` documents, so an
    explicit `-v` on the command line always beats a stale `PST_LOG_LEVEL` left
    in a shell profile. When both --quiet and -v/-vv are given, --quiet wins:
    an operator who explicitly asked for silence gets silence, even over a
    verbosity flag.

    The second element, wire_logs_on, says whether the WIRE_LOGGERS (boto3,
    httpx, etc.) should be turned on too — true only at -vv.

    The third element is a warning to emit once, or None. When no flag
    applies, an unparseable env value falls back to INFO and says so: a
    typo must never silence the tool. (If a flag does apply, the env var is
    never inspected, so it can't produce a warning either way.)
    """
    if quiet:
        return logging.WARNING, False, None
    if verbosity >= 2:
        return logging.DEBUG, True, None
    if verbosity == 1:
        return logging.DEBUG, False, None
    if env_value:
        level = _LEVELS.get(env_value.strip().lower())
        if level is None:
            valid = ", ".join(sorted(_LEVELS))
            return (
                DEFAULT_LEVEL,
                False,
                f"ignoring {ENV_LOG_LEVEL}={env_value!r}: not a known level. Valid: {valid}.",
            )
        return level, False, None
    return DEFAULT_LEVEL, False, None


#: Tokens shorter than this reveal too much of themselves in a 4-char tail.
_MIN_REDACTABLE_LEN = 12

#: Flags whose value is a secret and must never reach a log line or a
#: DEBUG-echoed argv.
_SECRET_FLAGS = ("--token",)


def get_logger(name: str) -> logging.Logger:
    """Logger for a module. Pass `__name__`; the package prefix does the rest."""
    return logging.getLogger(name)


def kv(**fields: object) -> str:
    """Render fields as a greppable `key=value` tail, skipping None values.

    Deliberately not JSON: these lines are read by a human in a terminal and
    grepped by the same human ten seconds later. A None value means "not
    applicable here" and is dropped rather than printed as `key=None`.
    """
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def redact(token: str | None) -> str:
    """Describe a token without disclosing it.

    Enough to answer "is it the token I think it is?" (tail + length) and never
    enough to use. Admin JWTs must not end up in a terminal scrollback, a
    screenshot, or a pasted bug report.
    """
    if not token:
        return "absent"
    if len(token) < _MIN_REDACTABLE_LEN:
        return f"present (len {len(token)})"
    return f"…{token[-4:]} (len {len(token)})"


def redact_argv(argv: Sequence[str]) -> list[str]:
    """argv with any secret flag's value masked, for the startup DEBUG line.

    Handles both `--token VALUE` and `--token=VALUE`. A trailing flag with no
    value is left alone rather than treated as an error — this runs on the
    happy path of every command and must never raise.
    """
    out: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            out.append("***")
            mask_next = False
            continue
        if arg in _SECRET_FLAGS:
            out.append(arg)
            mask_next = True
            continue
        if any(arg.startswith(f"{flag}=") for flag in _SECRET_FLAGS):
            out.append(f"{arg.split('=', 1)[0]}=***")
            continue
        out.append(arg)
    return out


@contextmanager
def timed(logger: logging.Logger, event: str, **fields: object) -> Iterator[dict[str, object]]:
    """Time a phase: DEBUG on entry, INFO on exit with `elapsed_ms`.

    Yields a dict the body can fill in with facts only known at the end (how
    many events were scanned, how many pages were fetched); those fields are
    merged into the closing line.

    The closing line is emitted from a `finally`, so a phase that raises still
    reports how long it ran — which is precisely the case an operator is
    debugging when a command appears to hang.
    """
    logger.debug("%s: start %s", event, kv(**fields))
    start = time.monotonic()
    extra: dict[str, object] = {}
    try:
        yield extra
    finally:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info("%s %s", event, kv(**fields, **extra, elapsed_ms=elapsed_ms))


class Heartbeat:
    """Rate-limited INFO progress for a loop that may run for minutes.

    Called on every iteration, emits at most one line per `interval` seconds:
    a fast run stays quiet, a slow one keeps proving it is alive. Plain log
    lines rather than a rich live widget, so progress survives being piped or
    redirected to a file.
    """

    def __init__(
        self,
        logger: logging.Logger,
        message: str,
        interval: float = 5.0,
        clock=time.monotonic,
    ) -> None:
        self._logger = logger
        self._message = message
        self._interval = interval
        self._clock = clock
        self._start = clock()
        self._last = self._start

    def tick(self, **fields: object) -> None:
        """Emit a progress line if the interval has elapsed; otherwise no-op."""
        now = self._clock()
        if now - self._last < self._interval:
            return
        self._last = now
        elapsed_s = round(now - self._start, 1)
        self._logger.info("  ...%s %s", self._message, kv(**fields, elapsed_s=elapsed_s))
