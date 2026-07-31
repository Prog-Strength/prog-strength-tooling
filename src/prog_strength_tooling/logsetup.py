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
    in a shell profile.

    The third element is a warning to emit once, or None. An unparseable env
    value falls back to INFO and says so: a typo must never silence the tool.
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
