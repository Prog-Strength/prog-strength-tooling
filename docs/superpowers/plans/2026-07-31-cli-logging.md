# CLI Logging & Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `pst` INFO-level phase/duration logging by default and DEBUG-level diagnostics on demand, and fix `pst whoop doctor`'s two uncapped CloudWatch scans so it can no longer hang silently.

**Architecture:** A new dependency-free `logsetup.py` owns level resolution, a `RichHandler` on the root logger writing to **stderr**, and two helpers (`timed`, `Heartbeat`) that every slow path uses. A new root `@app.callback()` in `cli.py` exposes `-v/-vv/-q`. Every module gets a module-level logger and instrumentation at the boundaries. Separately, `whooplogs.scan_deliveries` + `scan_syncs` collapse into one capped `scan()` that reports the timestamp range it actually covered.

**Tech Stack:** Python 3.12, Typer, rich (`RichHandler`), stdlib `logging`, pytest + `caplog`, `uv`.

**Spec:** `docs/superpowers/specs/2026-07-31-cli-logging-design.md`

**Repo rules that apply to every task** (from `AGENTS.md`):
- Work on branch `feat/cli-logging` (already cut from `main`). Never commit to `main`.
- Never edit `[project].version`, `__version__`, or `CHANGELOG.md` — python-semantic-release owns them.
- Before any commit, these must be green: `uv run black --check .`, `uv run ruff check .`, `uv run pytest`.
- Line length is 100 (black owns formatting; run `uv run black .` to fix).
- Commit messages follow Conventional Commits; a pre-commit hook enforces the format.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/prog_strength_tooling/logsetup.py` | **create** | Level resolution, handler install, `timed`, `Heartbeat`, `kv`, `redact`, `redact_argv`. Imports nothing from the package. |
| `src/prog_strength_tooling/cli.py` | modify | Root callback carrying `-v/-vv/-q`; startup DEBUG line. |
| `src/prog_strength_tooling/config.py` | modify | Log resolved environment/URLs/log-groups + precedence trace. |
| `src/prog_strength_tooling/window.py` | modify | Log the resolved absolute window. |
| `src/prog_strength_tooling/cloudwatch.py` | modify | Log client build, per-group scan, per-page detail, heartbeat, totals. |
| `src/prog_strength_tooling/client.py` | modify | Log each HTTP request/response with status + duration; log the configured timeout. |
| `src/prog_strength_tooling/health.py` | modify | DEBUG per-probe (INFO would duplicate the status table). |
| `src/prog_strength_tooling/whoop.py` | modify | DEBUG one line per check. Stays pure — no new I/O imports. |
| `src/prog_strength_tooling/whooplogs.py` | modify | `WhoopScan` + single-pass capped `scan()`; delete `scan_deliveries`/`scan_syncs`. |
| `src/prog_strength_tooling/render.py` | modify | Truncation banner + `"scan"` object in the diagnosis JSON. |
| `src/prog_strength_tooling/commands/whoop.py` | modify | Call `scan()` once; add `--max-events`; pass the scan to the renderer. |
| `tests/test_logsetup.py` | **create** | Level matrix, heartbeat, redaction, token-never-logged. |
| `tests/test_whooplogs.py` | modify | Rework onto `scan()`; add single-pagination and cap/coverage tests. |
| `tests/test_whoop_cli.py` | modify | Fixture returns a `WhoopScan`; `--max-events` and truncation banner tests. |
| `tests/test_cli.py` | modify | Root verbosity flags exist and don't disturb existing help. |
| `README.md` | modify | Document `-v/-vv/-q` and `PST_LOG_LEVEL`; add `logsetup.py` to the layout. |

---

## Task 1: `logsetup` — level resolution

**Files:**
- Create: `src/prog_strength_tooling/logsetup.py`
- Test: `tests/test_logsetup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_logsetup.py`:

```python
"""Logging setup: level resolution, redaction, and the progress heartbeat.

These are pure-function tests — no CLI runner, no handler side effects —
because level precedence is the part most likely to be silently wrong.
"""

import logging

import pytest

from prog_strength_tooling import logsetup


# --- level precedence: flags beat env, env beats the default --------------


@pytest.mark.parametrize(
    "verbosity,quiet,env,expected_level,expected_wire",
    [
        (0, False, None, logging.INFO, False),
        (1, False, None, logging.DEBUG, False),
        (2, False, None, logging.DEBUG, True),
        (3, False, None, logging.DEBUG, True),
        (0, True, None, logging.WARNING, False),
        # An explicit flag wins over the env var, in both directions.
        (1, False, "warning", logging.DEBUG, False),
        (0, True, "debug", logging.WARNING, False),
        # No flag: the env var decides, case-insensitively.
        (0, False, "debug", logging.DEBUG, False),
        (0, False, "DEBUG", logging.DEBUG, False),
        (0, False, " Warning ", logging.WARNING, False),
        (0, False, "error", logging.ERROR, False),
    ],
)
def test_resolve_level_precedence(verbosity, quiet, env, expected_level, expected_wire):
    level, wire, warning = logsetup.resolve_level(verbosity, quiet, env)
    assert level == expected_level
    assert wire is expected_wire
    assert warning is None


def test_unparseable_env_level_falls_back_to_info_with_a_warning():
    level, wire, warning = logsetup.resolve_level(0, False, "verbose")
    assert level == logging.INFO
    assert wire is False
    # A typo'd env var must never silence the tool, and must say what it saw.
    assert "verbose" in warning
    assert "debug" in warning


def test_empty_env_level_is_ignored():
    assert logsetup.resolve_level(0, False, "") == (logging.INFO, False, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_logsetup.py -v`
Expected: FAIL — `ImportError: cannot import name 'logsetup'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/prog_strength_tooling/logsetup.py`:

```python
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
import os
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_logsetup.py -v`
Expected: PASS — 13 passed (11 parametrized cases plus the two env-value tests).

- [ ] **Step 5: Commit**

```bash
uv run black src/prog_strength_tooling/logsetup.py tests/test_logsetup.py
git add src/prog_strength_tooling/logsetup.py tests/test_logsetup.py
git commit -m "feat(logging): add logsetup level resolution"
```

---

## Task 2: `logsetup` — formatting, redaction, timing, heartbeat

**Files:**
- Modify: `src/prog_strength_tooling/logsetup.py`
- Test: `tests/test_logsetup.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_logsetup.py`:

```python
# --- field rendering and redaction ----------------------------------------


def test_kv_renders_greppable_pairs_and_drops_none():
    assert logsetup.kv(group="/prog-strength/api", pages=3, empty=None) == (
        "group=/prog-strength/api pages=3"
    )


def test_kv_with_no_fields_is_empty():
    assert logsetup.kv() == ""


def test_redact_shows_only_the_tail_and_length():
    token = "eyJhbGciOiJIUzI1NiJ9.payload.sig9"
    out = logsetup.redact(token)
    assert out == f"…sig9 (len {len(token)})"
    assert "eyJhbGci" not in out


def test_redact_hides_short_tokens_entirely():
    # Too short for a 4-char tail to be safe — show nothing but the length.
    assert logsetup.redact("abc123") == "present (len 6)"


def test_redact_reports_a_missing_token():
    assert logsetup.redact(None) == "absent"
    assert logsetup.redact("") == "absent"


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["memory", "list", "--token", "secret-jwt"], ["memory", "list", "--token", "***"]),
        (["memory", "list", "--token=secret-jwt"], ["memory", "list", "--token=***"]),
        (["whoop", "doctor", "-v"], ["whoop", "doctor", "-v"]),
        # A trailing --token with no value must not blow up.
        (["memory", "list", "--token"], ["memory", "list", "--token"]),
    ],
)
def test_redact_argv_masks_token_values(argv, expected):
    assert logsetup.redact_argv(argv) == expected


# --- timing and progress --------------------------------------------------


def test_timed_logs_an_info_line_with_elapsed_and_extra_fields(caplog):
    log = logging.getLogger("prog_strength_tooling.test")
    with caplog.at_level(logging.INFO, logger="prog_strength_tooling.test"):
        with logsetup.timed(log, "whoop log scan", group="/prog-strength/api") as extra:
            extra.update(events=1204)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 1
    assert "whoop log scan" in messages[0]
    assert "group=/prog-strength/api" in messages[0]
    assert "events=1204" in messages[0]
    assert "elapsed_ms=" in messages[0]


def test_timed_still_logs_when_the_body_raises(caplog):
    """The failure case is exactly when you need to know how long it ran."""
    log = logging.getLogger("prog_strength_tooling.test")
    with caplog.at_level(logging.INFO, logger="prog_strength_tooling.test"):
        with pytest.raises(ValueError):
            with logsetup.timed(log, "whoop log scan"):
                raise ValueError("boom")

    assert any("whoop log scan" in r.getMessage() for r in caplog.records)


class _FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_heartbeat_emits_at_most_once_per_interval(caplog):
    log = logging.getLogger("prog_strength_tooling.test")
    clock = _FakeClock()
    beat = logsetup.Heartbeat(log, "scanning", interval=5.0, clock=clock)

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling.test"):
        beat.tick(pages=1)  # t=0: too soon after start, stays quiet
        clock.now = 2.0
        beat.tick(pages=2)  # still inside the interval
        clock.now = 6.0
        beat.tick(pages=3)  # first emission
        clock.now = 7.0
        beat.tick(pages=4)  # inside the interval again
        clock.now = 12.0
        beat.tick(pages=5)  # second emission

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 2
    assert "pages=3" in messages[0]
    assert "pages=5" in messages[1]
    assert "elapsed_s=" in messages[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_logsetup.py -v`
Expected: FAIL — `AttributeError: module 'prog_strength_tooling.logsetup' has no attribute 'kv'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/prog_strength_tooling/logsetup.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_logsetup.py -v`
Expected: PASS — all tests pass.

- [ ] **Step 5: Commit**

```bash
uv run black src/prog_strength_tooling/logsetup.py tests/test_logsetup.py
uv run ruff check src/prog_strength_tooling/logsetup.py
git add src/prog_strength_tooling/logsetup.py tests/test_logsetup.py
git commit -m "feat(logging): add kv/redact/timed/Heartbeat helpers"
```

---

## Task 3: `logsetup.configure` — install the handler

**Files:**
- Modify: `src/prog_strength_tooling/logsetup.py`
- Test: `tests/test_logsetup.py`

Background an implementer needs: pytest's `caplog` fixture works by attaching a handler to the **root** logger and relying on records propagating up to it. So `configure()` must (a) not set `propagate = False` on our logger, and (b) not remove handlers it did not install — otherwise it would rip out `caplog`'s handler mid-test and every log assertion in this repo would silently pass by capturing nothing. The implementation tags its own handler and only removes tagged ones.

Also note: during propagation Python checks **handler** levels, not ancestor **logger** levels. So attaching our handler to the root logger and setting per-logger levels is sufficient; root's own level only gates records logged directly on root.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_logsetup.py`:

```python
# --- handler installation -------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_logging():
    """Undo whatever configure() did, so tests don't leak handlers into each other."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    pst = logging.getLogger(logsetup.ROOT_LOGGER)
    saved_pst_level = pst.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    pst.setLevel(saved_pst_level)


def test_configure_sets_the_package_logger_level():
    logsetup.configure(verbosity=1, quiet=False)
    assert logging.getLogger(logsetup.ROOT_LOGGER).level == logging.DEBUG


def test_configure_is_idempotent():
    """Typer's CliRunner invokes the callback once per test; handlers must not stack."""
    logsetup.configure()
    logsetup.configure()
    logsetup.configure()
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_pst_cli", False)]
    assert len(ours) == 1


def test_configure_leaves_foreign_handlers_alone():
    """It must never remove pytest's caplog handler, or log assertions go blind."""
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    logsetup.configure()
    assert foreign in root.handlers


def test_configure_keeps_wire_loggers_quiet_by_default():
    logsetup.configure(verbosity=1, quiet=False)
    assert logging.getLogger("botocore").level == logging.WARNING


def test_double_verbose_turns_on_wire_loggers():
    logsetup.configure(verbosity=2, quiet=False)
    for name in logsetup.WIRE_LOGGERS:
        assert logging.getLogger(name).level == logging.DEBUG


def test_configure_warns_once_about_a_bad_env_level(monkeypatch, caplog):
    monkeypatch.setenv(logsetup.ENV_LOG_LEVEL, "chatty")
    with caplog.at_level(logging.WARNING, logger=logsetup.ROOT_LOGGER):
        logsetup.configure()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "chatty" in warnings[0]


def test_configure_reads_the_env_level(monkeypatch):
    monkeypatch.setenv(logsetup.ENV_LOG_LEVEL, "error")
    logsetup.configure()
    assert logging.getLogger(logsetup.ROOT_LOGGER).level == logging.ERROR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_logsetup.py -k configure -v`
Expected: FAIL — `AttributeError: module 'prog_strength_tooling.logsetup' has no attribute 'configure'`.

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `src/prog_strength_tooling/logsetup.py`:

```python
from rich.console import Console
from rich.logging import RichHandler
```

Append to `src/prog_strength_tooling/logsetup.py`:

```python
#: Marks the handler this module installs, so `configure` can replace its own
#: handler without touching anyone else's — notably pytest's caplog handler,
#: which lives on the root logger and would otherwise be removed, silently
#: blinding every log assertion in the suite.
_HANDLER_ATTR = "_pst_cli"


def configure(verbosity: int = 0, quiet: bool = False) -> None:
    """Install the CLI's log handler and set levels. Safe to call repeatedly.

    The handler goes on the **root** logger, writing to stderr, and per-logger
    levels decide what reaches it (during propagation Python checks handler
    levels, not ancestor logger levels). Our own logger is left propagating so
    pytest's caplog — and any future embedder — still sees the records.
    """
    level, wire, warning = resolve_level(verbosity, quiet, os.getenv(ENV_LOG_LEVEL))

    handler = RichHandler(
        console=Console(stderr=True),
        show_path=False,
        # Log lines are data, not markup: an evidence string like
        # "/webhooks/whoop," or a message containing "[dim]" must render
        # literally rather than be swallowed as a rich tag.
        markup=False,
        rich_tracebacks=True,
        log_time_format="%H:%M:%S",
    )
    # At DEBUG the speaking module is the point; at INFO it is noise.
    fmt = "[%(name)s] %(message)s" if level <= logging.DEBUG else "%(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    setattr(handler, _HANDLER_ATTR, True)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_ATTR, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    # Only gates records logged directly on root (nothing here does); keeps a
    # stray third-party WARNING visible without opening the floodgates.
    root.setLevel(logging.WARNING)

    logging.getLogger(ROOT_LOGGER).setLevel(level)
    for name in WIRE_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG if wire else logging.WARNING)

    if warning:
        logging.getLogger(ROOT_LOGGER).warning(warning)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_logsetup.py -v`
Expected: PASS — all tests pass.

- [ ] **Step 5: Commit**

```bash
uv run black src/prog_strength_tooling/logsetup.py tests/test_logsetup.py
uv run ruff check .
git add src/prog_strength_tooling/logsetup.py tests/test_logsetup.py
git commit -m "feat(logging): install a stderr rich log handler"
```

---

## Task 4: Root callback — `-v` / `-vv` / `-q`

**Files:**
- Modify: `src/prog_strength_tooling/cli.py`
- Test: `tests/test_cli.py`

Note on Typer: `typer.Typer(help=HELP)` and a callback docstring both want to be the app's help text, and which wins is version-dependent. Pass `help=HELP` explicitly to `@app.callback(...)` so the existing help text is guaranteed to survive.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py` already defines `runner = CliRunner()` but has no ANSI-stripping helper. Add `import re` to its imports and this helper below `runner`:

```python
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI colour so help-text assertions don't depend on whether the
    runner (locally) or CI (FORCE_COLOR) renders rich output with escapes."""
    return _ANSI.sub("", text)
```

Then append:

```python
def test_root_help_still_shows_the_app_description():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = _plain(result.output)
    assert "CLI tooling for Prog Strength Backend" in out
    assert "--verbose" in out
    assert "--quiet" in out


def test_verbose_flag_is_accepted_before_a_subcommand():
    result = runner.invoke(app, ["-v", "status", "--help"])
    assert result.exit_code == 0


def test_verbose_flag_sets_the_debug_level():
    import logging

    from prog_strength_tooling import logsetup

    runner.invoke(app, ["-v", "status", "--help"])
    assert logging.getLogger(logsetup.ROOT_LOGGER).level == logging.DEBUG


def test_quiet_flag_sets_the_warning_level():
    import logging

    from prog_strength_tooling import logsetup

    runner.invoke(app, ["-q", "status", "--help"])
    assert logging.getLogger(logsetup.ROOT_LOGGER).level == logging.WARNING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `--verbose` is not in the help output, and the level assertions fail (level is unset/`NOTSET`).

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/cli.py`, add to the imports:

```python
import platform
import sys

from . import logsetup
from . import __version__
from .commands import logs, memory, status, whoop
```

Then, immediately after the `app = typer.Typer(...)` block and **before** the `add_typer` calls, insert:

```python
@app.callback(help=HELP)
def main(
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="More log detail: [cyan]-v[/cyan] for pst debug logs, "
        "[cyan]-vv[/cyan] to add AWS/HTTP wire logs.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Log only warnings and errors.",
    ),
) -> None:
    """Configure logging before any subcommand runs.

    Logs go to stderr at INFO by default, so `--json` on stdout stays pipeable
    and a long-running command still shows what phase it is in. Set
    PST_LOG_LEVEL instead of passing a flag every time; an explicit flag wins.
    """
    logsetup.configure(verbosity=verbose, quiet=quiet)
    log = logsetup.get_logger(__name__)
    log.debug(
        "pst starting %s",
        logsetup.kv(version=__version__, python=platform.python_version()),
    )
    log.debug("argv: %s", " ".join(logsetup.redact_argv(sys.argv[1:])))
```

Extend the `EPILOG` env-var line — replace the existing final `[dim]Env vars: …[/dim]` paragraph with:

```python
[dim]Env vars: PST_ENV · PST_TOKEN (admin JWT, memory only) · PST_API_URL · \
PST_AWS_PROFILE (logs only) · PST_LOG_LEVEL (debug/info/warning/error). \
Run 'pst COMMAND --help' for per-command options.[/dim]"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v && uv run pytest -q`
Expected: PASS — the new tests pass and the whole suite stays green.

- [ ] **Step 5: Commit**

```bash
uv run black . && uv run ruff check .
git add src/prog_strength_tooling/cli.py tests/test_cli.py
git commit -m "feat(logging): add -v/-vv/-q verbosity flags to pst"
```

---

## Task 5: Instrument `config.py` and `window.py`

**Files:**
- Modify: `src/prog_strength_tooling/config.py`
- Modify: `src/prog_strength_tooling/window.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py` imports names directly (`resolve`, `resolve_admin`, …) rather than the module, and has an autouse `_clean_env` fixture that clears every `PST_*` var. Add `resolve_logs` to its existing `from prog_strength_tooling.config import (...)` block, then append:

```python
def test_resolve_logs_logs_the_environment_at_info(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        resolve_logs("prod", None, "my-profile", None)

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "env=prod" in line
    assert "region=us-east-2" in line
    assert "profile=my-profile" in line


def test_resolve_admin_never_logs_the_token(caplog):
    import logging

    secret = "eyJhbGciOiJIUzI1NiJ9.super-secret-payload.signature"
    with caplog.at_level(logging.DEBUG, logger="prog_strength_tooling"):
        resolve_admin(None, secret, "prod")

    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "super-secret-payload" not in record.getMessage()
```

Also append to `tests/test_window.py` (it imports `resolve` from `prog_strength_tooling.window`):

```python
def test_resolve_logs_the_absolute_window_at_info(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        resolve("7d", None, None)

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "since=7d" in line
    assert "start=" in line
    assert "end=" in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k "logs_the_environment or never_logs_the_token" -v`
Expected: FAIL — `test_resolve_logs_logs_the_environment_at_info` fails because no records are emitted (`assert "env=prod" in ""`).

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/config.py`, add after the existing imports:

```python
from .logsetup import get_logger, kv, redact

log = get_logger(__name__)
```

In `resolve_environment`, replace the body with:

```python
def resolve_environment(env: str | None) -> Environment:
    """Resolve the active environment (--env -> PST_ENV -> default) + its URLs."""
    name = env or os.getenv(ENV_ENV) or DEFAULT_ENVIRONMENT
    log.debug(
        "resolving environment %s",
        kv(flag=env, env_var=os.getenv(ENV_ENV), default=DEFAULT_ENVIRONMENT, chosen=name),
    )
    services = services_for(name)
    assert services is not None  # name is always non-empty here
    return Environment(name=name, services=services)
```

In `resolve`, replace the body with:

```python
def resolve(api: str | None, token: str | None, env: str | None = None) -> Config:
    """Build a Config for the memory commands following the documented precedence."""
    api_url = (
        api
        or (services_for(env) or {}).get("api")
        or os.getenv(ENV_API_URL)
        or (services_for(os.getenv(ENV_ENV)) or {}).get("api")
        or NAMED_ENVIRONMENTS[DEFAULT_ENVIRONMENT]["api"]
    )
    resolved_token = token or os.getenv(ENV_TOKEN) or None
    log.debug(
        "resolved api config %s",
        kv(
            api_flag=api,
            env_flag=env,
            api_env_var=os.getenv(ENV_API_URL),
            api_url=api_url,
            token=redact(resolved_token),
            token_source=("flag" if token else "env" if os.getenv(ENV_TOKEN) else "none"),
        ),
    )
    log.info("api %s", kv(url=api_url))
    return Config(api_url=api_url, token=resolved_token)
```

In `resolve_logs`, add immediately before the closing `return LogsConfig(...)`:

```python
    resolved_profile = profile or os.getenv(ENV_AWS_PROFILE) or None
    resolved_region = region or AWS_REGION
    log.info(
        "cloudwatch %s",
        kv(
            env=environment.name,
            region=resolved_region,
            profile=resolved_profile or "default-chain",
            groups=",".join(log_group_for(s) for s in SERVICES if s in chosen),
        ),
    )
    return LogsConfig(
        environment=environment.name,
        region=resolved_region,
        profile=resolved_profile,
        log_groups={s: log_group_for(s) for s in SERVICES if s in chosen},
    )
```

(Delete the previous `return LogsConfig(...)` block so `profile` and `region` are computed once.)

In `src/prog_strength_tooling/window.py`, add after the existing imports:

```python
from .logsetup import get_logger, kv

log = get_logger(__name__)
```

and add these two lines immediately before each `return Window(...)` in `resolve`:

```python
        log.info(
            "window %s",
            kv(start=resolved_start.isoformat(), end=resolved_end.isoformat(), since=None),
        )
        return Window(start=resolved_start, end=resolved_end, since=None)
```

```python
    spec = since or "24h"
    start = now - parse_duration(spec)
    log.info("window %s", kv(since=spec, start=start.isoformat(), end=now.isoformat()))
    return Window(start=start, end=now, since=spec)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py tests/test_window.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run black . && uv run ruff check . && uv run pytest -q
git add src/prog_strength_tooling/config.py src/prog_strength_tooling/window.py tests/test_config.py
git commit -m "feat(logging): log resolved config and query window"
```

---

## Task 6: Instrument `cloudwatch.py`

**Files:**
- Modify: `src/prog_strength_tooling/cloudwatch.py`
- Test: `tests/test_cloudwatch.py`

- [ ] **Step 1: Write the failing test**

`tests/test_cloudwatch.py` already defines everything this test needs — the `fake_client` fixture, `_event`, `_page`, `CFG`, and `WINDOW`. Append:

```python
def test_search_logs_progress_and_totals(fake_client, caplog):
    import logging

    fake_client([_page(_event(1_000, "req-abc started"))])
    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        cloudwatch.search(CFG, "req-abc", WINDOW, 500)

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "/prog-strength/api" in line
    assert "elapsed_ms=" in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cloudwatch.py -k progress -v`
Expected: FAIL — `assert '/prog-strength/api' in ''`, since nothing is logged.

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/cloudwatch.py`, add after the existing imports:

```python
from .logsetup import Heartbeat, get_logger, kv, timed

log = get_logger(__name__)
```

In `build_client`, add right before the `return session.client("logs")`:

```python
        log.debug(
            "building AWS client %s",
            kv(profile=cfg.profile or "default-chain", region=cfg.region),
        )
```

Replace the body of `_search_group` (keeping its docstring) with:

```python
    records: list[LogRecord] = []
    paginator = client.get_paginator("filter_log_events")
    pages = paginator.paginate(
        logGroupName=group,
        startTime=window.start_ms,
        endTime=window.end_ms,
        filterPattern=f'"{request_id}"',
        # Stop paging once we have enough. One MORE than the limit, so the
        # merge step can still tell "exactly limit lines exist" from "there
        # were more and we cut them" — capping at exactly `limit` here would
        # make every truncated result look complete.
        PaginationConfig={"MaxItems": limit + 1},
    )

    beat = Heartbeat(log, f"searching {group}")
    page_count = 0
    scanned = 0
    with timed(log, "log group search", group=group) as summary:
        for page in pages:
            page_count += 1
            events = page.get("events", [])
            scanned += len(events)
            for event in events:
                message = event.get("message", "")
                if request_id not in message:
                    continue
                records.append(
                    parse(
                        service=service,
                        timestamp=datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC),
                        message=message,
                        stream=event.get("logStreamName", ""),
                    )
                )
            log.debug(
                "page fetched %s",
                kv(group=group, page=page_count, events=len(events), matched=len(records)),
            )
            beat.tick(pages=page_count, events=scanned)
        summary.update(pages=page_count, scanned=scanned, matched=len(records))

    return records
```

In `search`, wrap the fan-out — replace the `with ThreadPoolExecutor(...)` block with:

```python
    log.info("searching %d log group(s) %s", len(groups), kv(window=window.describe()))
    # boto3 clients are safe to call from multiple threads; only creating them
    # is not, and that already happened above.
    with timed(log, "cloudwatch search", groups=len(groups)) as summary:
        with ThreadPoolExecutor(max_workers=max(len(groups), 1)) as pool:
            results = list(pool.map(run, groups))
        summary.update(matched=sum(len(records) for _, records in results))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cloudwatch.py tests/test_logs_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run black . && uv run ruff check . && uv run pytest -q
git add src/prog_strength_tooling/cloudwatch.py tests/test_cloudwatch.py
git commit -m "feat(logging): log CloudWatch paging progress and totals"
```

---

## Task 7: Instrument `client.py`, `health.py`, and `whoop.py`

**Files:**
- Modify: `src/prog_strength_tooling/client.py`
- Modify: `src/prog_strength_tooling/health.py`
- Modify: `src/prog_strength_tooling/whoop.py`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_client.py` (the file already uses respx; reuse its existing `BASE`/`Config` setup — the snippet below assumes `Config` and `respx` are imported there):

```python
@respx.mock
def test_client_logs_method_path_status_and_duration(caplog):
    import logging

    respx.get("https://api.progstrength.fitness/admin/memories").mock(
        return_value=httpx.Response(200, json={"data": {"memories": [], "count": 0}})
    )
    cfg = Config(api_url="https://api.progstrength.fitness", token="admin-token-value-1234")

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        with MemoryClient(cfg) as client:
            client.list_memories(user_id="u1")

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "GET" in line
    assert "/admin/memories" in line
    assert "status=200" in line
    assert "elapsed_ms=" in line


@respx.mock
def test_client_never_logs_the_bearer_token(caplog):
    import logging

    secret = "eyJhbGciOiJIUzI1NiJ9.admin-token-payload.sig"
    respx.get("https://api.progstrength.fitness/admin/memories").mock(
        return_value=httpx.Response(200, json={"data": {"memories": [], "count": 0}})
    )
    cfg = Config(api_url="https://api.progstrength.fitness", token=secret)

    with caplog.at_level(logging.DEBUG, logger="prog_strength_tooling"):
        with MemoryClient(cfg) as client:
            client.list_memories(user_id="u1")

    for record in caplog.records:
        assert secret not in record.getMessage()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_client.py -k "logs_method or never_logs" -v`
Expected: FAIL — `assert 'GET' in ''`.

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/client.py`, add after the existing imports:

```python
import time

from .logsetup import get_logger, kv, redact

log = get_logger(__name__)
```

Add a module-level helper below the exception classes:

```python
def _request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    """Issue a request, logging the outcome with its duration.

    The duration is the single most useful field here: a command that appears
    hung is usually sitting inside one of these calls waiting out the client
    timeout, and the WARNING on failure prints how long it actually waited.
    The Authorization header is never logged — only the path, status, and time.
    """
    log.debug("request %s", kv(method=method, path=path, **kwargs.get("params", {}) or {}))
    start = time.monotonic()
    try:
        resp = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        log.warning(
            "%s %s failed %s",
            method,
            path,
            kv(error=type(exc).__name__, elapsed_ms=elapsed_ms),
        )
        raise
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    log.info(
        "%s %s %s",
        method,
        path,
        kv(status=resp.status_code, bytes=len(resp.content), elapsed_ms=elapsed_ms),
    )
    return resp
```

In **both** `MemoryClient.__init__` and `WhoopAdminClient.__init__`, add immediately after the `self._client = httpx.Client(...)` assignment:

```python
        log.debug(
            "%s ready %s",
            type(self).__name__,
            kv(base_url=cfg.base_url, timeout_s=timeout, token=redact(cfg.token)),
        )
```

In **all four** transport methods (`MemoryClient._get`, `MemoryClient._post`, `WhoopAdminClient._get`, `WhoopAdminClient._post`), replace the `self._client.get(...)` / `self._client.post(...)` call with the helper. For example, `MemoryClient._get` becomes:

```python
    def _get(self, path: str, params: dict[str, str | int]) -> dict:
        try:
            resp = _request(self._client, "GET", path, params=params)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return self._unwrap(resp)
```

and `MemoryClient._post` becomes:

```python
    def _post(self, path: str, body: dict[str, object]) -> dict:
        try:
            resp = _request(self._client, "POST", path, json=body)
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc
        return self._unwrap(resp)
```

Apply the identical two changes to `WhoopAdminClient._get` and `WhoopAdminClient._post` (they return `MemoryClient._unwrap(resp)` rather than `self._unwrap(resp)` — keep that as-is).

In `src/prog_strength_tooling/health.py`, add after the existing imports:

```python
from .logsetup import get_logger, kv

log = get_logger(__name__)
```

and add at the start of `check`, right after `url = base_url.rstrip("/") + "/health"`:

```python
    log.debug("probing %s", kv(service=name, url=url, timeout_s=timeout))
```

and immediately before the final `return ServiceStatus(` in `check`:

```python
    log.debug(
        "probe ok %s",
        kv(service=name, version=body.get("version"), latency_ms=round(latency_ms, 1)),
    )
```

In `src/prog_strength_tooling/whoop.py`, add after the existing imports:

```python
from .logsetup import get_logger, kv

log = get_logger(__name__)
```

`diagnose` currently builds its list inline inside the `Diagnosis(checks=[...])` constructor call. Replace its `return` statement (currently `whoop.py:339-349`) with a named list so each result can be logged:

```python
    checks = [
        _check_deliveries_arriving(deliveries),
        _check_delivery_path(deliveries),
        _check_signatures_accepted(deliveries),
        _check_deliveries_producing_syncs(deliveries, syncs),
        _check_syncs_landing_rows(syncs),
        _check_connection_health(connection, token_present),
        _check_data_freshness(connection, token_present, now),
    ]
    for check in checks:
        log.debug(
            "check %s",
            kv(
                name=check.name,
                ok=check.ok,
                skipped=check.skipped,
                reason=check.reason,
                symptom=check.finding.symptom if check.finding else None,
            ),
        )
    return Diagnosis(checks=checks)
```

`whoop.py` must remain pure — `logsetup` imports only stdlib and rich, so this adds no I/O dependency and the module still imports no boto3/httpx.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_client.py tests/test_health.py tests/test_whoop_diagnosis.py tests/test_status_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run black . && uv run ruff check . && uv run pytest -q
git add src/prog_strength_tooling/client.py src/prog_strength_tooling/health.py src/prog_strength_tooling/whoop.py tests/test_client.py
git commit -m "feat(logging): log HTTP requests, health probes, and doctor checks"
```

---

## Task 8: `whooplogs.scan()` — one capped pass

**Files:**
- Modify: `src/prog_strength_tooling/whooplogs.py`
- Test: `tests/test_whooplogs.py`

This task **adds** `scan()` alongside the existing `scan_deliveries` / `scan_syncs`, so every commit stays green. Task 10 switches the command over and deletes the old pair.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_whooplogs.py`:

```python
# --- single-pass scan -----------------------------------------------------


def test_scan_serves_both_aggregations_from_one_pagination(fake_client):
    """The whole point of the rewrite: one AWS pass, not two."""
    client = fake_client([_healthy_page()])
    result = whooplogs.scan(CFG, WINDOW)

    assert len(client.paginator.calls) == 1
    assert result.deliveries.groups[0].uri == "/webhooks/whoop"
    assert result.deliveries.groups[0].count == 5
    assert result.syncs.window_sync_count == 2
    assert result.syncs.upserted_total == 5


def test_scan_reports_events_pages_and_is_not_truncated(fake_client):
    fake_client([_healthy_page()])
    result = whooplogs.scan(CFG, WINDOW)
    assert result.events_scanned == 8
    assert result.pages == 1
    assert result.truncated is False


def test_scan_records_the_timestamp_range_it_actually_covered(fake_client):
    fake_client([_healthy_page()])
    result = whooplogs.scan(CFG, WINDOW)
    # _healthy_page spans event timestamps 1000ms..7000ms.
    assert result.covered_start.timestamp() == pytest.approx(1.0)
    assert result.covered_end.timestamp() == pytest.approx(7.0)


def test_scan_stops_at_max_events_and_marks_truncation(fake_client):
    good = "http://api.progstrength.fitness/webhooks/whoop"
    pages = [
        _page(*[_event(1_000 + i, _request_line("POST", good, 204)) for i in range(10)]),
        _page(*[_event(2_000 + i, _request_line("POST", good, 204)) for i in range(10)]),
        _page(*[_event(3_000 + i, _request_line("POST", good, 204)) for i in range(10)]),
    ]
    fake_client(pages)
    result = whooplogs.scan(CFG, WINDOW, max_events=15)

    assert result.truncated is True
    assert result.events_scanned == 15
    # Stopped partway through page 2, so page 3 was never counted.
    assert result.pages == 2
    # Coverage reflects real event timestamps, NOT the requested window: a
    # capped scan keeps the OLDEST slice, since FilterLogEvents returns events
    # ascending by timestamp.
    assert result.covered_end.timestamp() == pytest.approx(2.004)
    assert result.covered_end.timestamp() < WINDOW.end.timestamp()


def test_scan_with_max_events_zero_scans_everything(fake_client):
    good = "http://api.progstrength.fitness/webhooks/whoop"
    pages = [
        _page(*[_event(1_000 + i, _request_line("POST", good, 204)) for i in range(10)]),
        _page(*[_event(2_000 + i, _request_line("POST", good, 204)) for i in range(10)]),
    ]
    fake_client(pages)
    result = whooplogs.scan(CFG, WINDOW, max_events=0)
    assert result.truncated is False
    assert result.events_scanned == 20


def test_scan_warns_when_truncated(fake_client, caplog):
    import logging

    good = "http://api.progstrength.fitness/webhooks/whoop"
    fake_client([_page(*[_event(1_000 + i, _request_line("POST", good, 204)) for i in range(10)])])
    with caplog.at_level(logging.WARNING, logger="prog_strength_tooling"):
        whooplogs.scan(CFG, WINDOW, max_events=5)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "max-events" in warnings[0]
    assert "covered" in warnings[0]


def test_scan_on_an_empty_window_has_no_coverage(fake_client):
    fake_client([_page()])
    result = whooplogs.scan(CFG, WINDOW)
    assert result.events_scanned == 0
    assert result.covered_start is None
    assert result.covered_end is None
    assert result.truncated is False


def test_scan_queries_only_the_api_group_with_a_quoted_whoop_pattern(fake_client):
    client = fake_client([_page()])
    whooplogs.scan(CFG, WINDOW)
    call = client.paginator.calls[0]
    assert call["logGroupName"] == "/prog-strength/api"
    assert call["filterPattern"] == '"whoop"'
    assert call["startTime"] == WINDOW.start_ms
    assert call["endTime"] == WINDOW.end_ms
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_whooplogs.py -k scan_serves -v`
Expected: FAIL — `AttributeError: module 'prog_strength_tooling.whooplogs' has no attribute 'scan'`.

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/whooplogs.py`, extend the imports:

```python
from datetime import UTC, datetime

from .logsetup import Heartbeat, get_logger, kv, timed

log = get_logger(__name__)
```

Add below the existing `SyncScan` dataclass:

```python
#: Default ceiling on how many log events one doctor run will read. A 7-day
#: window over a busy api log group can hold far more "whoop" lines than the
#: diagnosis needs, and an uncapped paginator is how `doctor` came to look like
#: it had hung. 0 (via --max-events 0) restores the unbounded scan.
MAX_EVENTS = 20_000


@dataclass(frozen=True)
class WhoopScan:
    """Both aggregations plus what the single pass actually managed to read.

    `covered_start` / `covered_end` are the MIN and MAX event timestamps seen,
    not the requested window. They differ whenever the cap trips, and they are
    measured rather than assumed because FilterLogEvents returns events
    ascending by timestamp — so a capped scan keeps the OLDEST slice of the
    window, which is the opposite of what a freshness check wants. Reporting
    the real range is the only honest option.
    """

    deliveries: DeliveryScan
    syncs: SyncScan
    events_scanned: int
    pages: int
    truncated: bool
    covered_start: datetime | None
    covered_end: datetime | None

    def describe_coverage(self) -> str:
        """Human phrasing of the range actually read, for the operator banner."""
        if self.covered_start is None or self.covered_end is None:
            return "no matching events"
        return (
            f"{self.covered_start:%Y-%m-%d %H:%M}Z to {self.covered_end:%Y-%m-%d %H:%M}Z"
        )
```

Change `_paginate_whoop` to accept and forward the cap:

```python
def _paginate_whoop(client, group: str, window: Window, max_events: int):
    """Yield each page of "whoop" events in the api log group over the window.

    Yields whole pages rather than individual events so the caller can count
    pages for its progress heartbeat and stop mid-page when the cap trips.

    A single literal-substring filterPattern ('"whoop"') server-side, matching
    cloudwatch.py's quoted-pattern convention and its window -> startTime/
    endTime handling (epoch millis). Both request lines and sync lines carry
    the substring, so one pass feeds both aggregations.

    `max_events` (0 = unlimited) is passed as MaxItems so botocore stops
    fetching server-side too, mirroring cloudwatch._search_group; the caller
    still counts client-side, because only the caller can tell the difference
    between "that was everything" and "we cut it short".
    """
    paginator = client.get_paginator("filter_log_events")
    kwargs: dict[str, object] = {
        "logGroupName": group,
        "startTime": window.start_ms,
        "endTime": window.end_ms,
        "filterPattern": '"whoop"',
    }
    if max_events:
        kwargs["PaginationConfig"] = {"MaxItems": max_events + 1}
    pages = paginator.paginate(**kwargs)
    for page in pages:
        yield page
```

Add the two accumulator helpers and `scan` at the end of the module:

```python
def _count_delivery(message: str, counts: dict[tuple[str, int], int]) -> None:
    """Fold one log line into the (uri, status) delivery buckets, if it is one.

    Only POSTs whose path contains "whoop" (case-insensitive) count — a GET
    health probe or a strava webhook that happened to share the page is not a
    whoop delivery. Lines that don't parse as a request line are skipped (a
    sync line, an OAuth log): dropping them is right, not an error, since the
    "whoop" filter deliberately pulls in more than request lines.
    """
    match = _REQUEST_RE.search(message)
    if match is None:
        return
    if match.group("method") != "POST":
        return
    uri = _uri_path(match.group("url"))
    if "whoop" not in uri.lower():
        return
    key = (uri, int(match.group("status")))
    counts[key] = counts.get(key, 0) + 1


def scan(cfg: LogsConfig, window: Window, max_events: int = MAX_EVENTS) -> WhoopScan:
    """Read the api log group once and build both scans from the same events.

    Previously the doctor called two functions that each paginated the same
    query over the same window — twice the AWS calls and twice the wait, with
    no progress output and no ceiling. One capped pass replaces both.
    """
    client = cloudwatch.build_client(cfg)
    group = log_group_for("api")

    counts: dict[tuple[str, int], int] = {}
    window_sync_count = 0
    upserted_total = 0
    events_scanned = 0
    pages = 0
    truncated = False
    first_ms: int | None = None
    last_ms: int | None = None

    log.info(
        "scanning %s for whoop evidence %s",
        group,
        kv(window=window.describe(), max_events=max_events or "unlimited"),
    )
    beat = Heartbeat(log, f"scanning {group}")

    with timed(log, "whoop log scan", group=group) as summary:
        try:
            for page in _paginate_whoop(client, group, window, max_events):
                pages += 1
                events = page.get("events", [])
                for event in events:
                    events_scanned += 1
                    stamp = event.get("timestamp")
                    if isinstance(stamp, int):
                        first_ms = stamp if first_ms is None else min(first_ms, stamp)
                        last_ms = stamp if last_ms is None else max(last_ms, stamp)

                    message = event.get("message", "")
                    _count_delivery(message, counts)
                    record = _parse_sync(message)
                    if record is not None and record.get("kind") == WINDOW_KIND:
                        window_sync_count += 1
                        upserted = record.get("upserted", 0)
                        if isinstance(upserted, int):
                            upserted_total += upserted

                    if max_events and events_scanned >= max_events:
                        truncated = True
                        break

                log.debug(
                    "page fetched %s",
                    kv(page=pages, events=len(events), total=events_scanned),
                )
                beat.tick(pages=pages, events=events_scanned)
                if truncated:
                    break
        except (ClientError, BotoCoreError) as exc:
            # Reuse the shared translation so credential/permission/region
            # errors read the same here as in `pst logs`.
            raise cloudwatch.CloudWatchError(cloudwatch.describe_failure(exc, group, cfg)) from exc

        summary.update(pages=pages, events=events_scanned, truncated=truncated)

    # Deterministic order: busiest bucket first, then uri for a stable tie-break
    # so repeated runs and test assertions don't depend on dict/insertion order.
    groups = [
        DeliveryGroup(uri=uri, status=status, count=count)
        for (uri, status), count in counts.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.uri, g.status))

    result = WhoopScan(
        deliveries=DeliveryScan(groups=groups),
        syncs=SyncScan(window_sync_count=window_sync_count, upserted_total=upserted_total),
        events_scanned=events_scanned,
        pages=pages,
        truncated=truncated,
        covered_start=_as_dt(first_ms),
        covered_end=_as_dt(last_ms),
    )

    if truncated:
        log.warning(
            "scan hit --max-events %d after %d pages; covered %s of the requested %s. "
            "Narrow --since, or pass --max-events 0 to scan the whole window.",
            max_events,
            pages,
            result.describe_coverage(),
            window.describe(),
        )

    return result


def _as_dt(stamp_ms: int | None) -> datetime | None:
    """CloudWatch epoch-millis to an aware UTC datetime, or None."""
    return None if stamp_ms is None else datetime.fromtimestamp(stamp_ms / 1000, tz=UTC)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_whooplogs.py -v`
Expected: PASS — the new `scan` tests pass and the existing `scan_deliveries` / `scan_syncs` tests still pass.

- [ ] **Step 5: Commit**

```bash
uv run black . && uv run ruff check . && uv run pytest -q
git add src/prog_strength_tooling/whooplogs.py tests/test_whooplogs.py
git commit -m "feat(whoop): add a single-pass capped scan of the whoop log evidence"
```

---

## Task 9: Truncation banner in `render_diagnosis`

**Files:**
- Modify: `src/prog_strength_tooling/render.py`
- Test: `tests/test_whoop_cli.py`

`render_diagnosis` gains a required positional `scan` parameter. Its only caller is `commands/whoop.py`, updated in the same task so the suite stays green.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_whoop_cli.py`, and add `WhoopScan` to its `from prog_strength_tooling.whooplogs import ...` line:

```python
def _scan(deliveries, syncs, *, truncated=False, events=1204, pages=3):
    """A WhoopScan wrapping fixture aggregations, as the command would build it."""
    from datetime import UTC, datetime

    return WhoopScan(
        deliveries=deliveries,
        syncs=syncs,
        events_scanned=events,
        pages=pages,
        truncated=truncated,
        covered_start=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
        covered_end=datetime(2026, 7, 30, 9, 15, tzinfo=UTC),
    )


def _diagnose(scan):
    """Run the engine over a fixture scan with no admin evidence (checks 6/7 skip).

    `diagnose` takes `now` positionally with no default, so it is passed here.
    """
    from datetime import UTC, datetime

    from prog_strength_tooling.whoop import diagnose

    return diagnose(scan.deliveries, scan.syncs, None, False, datetime.now(UTC))


def test_diagnosis_shows_a_truncation_banner(capsys):
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    render_diagnosis(_diagnose(scan), scan, as_json=False)

    out = _plain(capsys.readouterr().out)
    assert "results truncated" in out
    assert "2026-07-29 08:00Z" in out


def test_diagnosis_has_no_banner_when_complete(capsys):
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    render_diagnosis(_diagnose(scan), scan, as_json=False)

    assert "truncated" not in _plain(capsys.readouterr().out)


def test_diagnosis_json_carries_the_scan_metadata(capsys):
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    render_diagnosis(_diagnose(scan), scan, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["scan"]["truncated"] is True
    assert payload["scan"]["events_scanned"] == 1204
    assert payload["scan"]["pages"] == 3
    assert payload["scan"]["covered_start"].startswith("2026-07-29T08:00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_whoop_cli.py -k "truncation or scan_metadata" -v`
Expected: FAIL — `TypeError: render_diagnosis() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/render.py`, extend the imports:

```python
from .whooplogs import WhoopScan
```

Change the signature and add the two blocks:

```python
def render_diagnosis(diagnosis: Diagnosis, scan: WhoopScan, *, as_json: bool) -> None:
    """Render the WHOOP doctor's seven checks: one line each, findings expanded.

    Everything goes to stdout via `console` so `--json` pipes cleanly (the
    error path writes to `err_console` in the command layer, not here). Finding
    prose is rendered as plain Text so evidence containing a stray bracket or a
    "/webhooks/whoop," path is shown literally, never read as rich markup.

    The scan is carried alongside the diagnosis so a truncated read is never
    presented as a complete one — a partial diagnosis that looks whole is worse
    than no diagnosis, because "no deliveries found" would read as evidence.
    """
```

Inside the `if as_json:` branch, add a `"scan"` key to the dict passed to `json.dumps` (alongside `"healthy"`, `"checks"`, and `"findings"`):

```python
                    "scan": {
                        "events_scanned": scan.events_scanned,
                        "pages": scan.pages,
                        "truncated": scan.truncated,
                        "covered_start": (
                            scan.covered_start.isoformat() if scan.covered_start else None
                        ),
                        "covered_end": (
                            scan.covered_end.isoformat() if scan.covered_end else None
                        ),
                    },
```

Immediately after the `if as_json: ... return` block and **before** the `for c in diagnosis.checks:` loop, add:

```python
    if scan.truncated:
        banner = Text()
        banner.append("! ", style="yellow")
        banner.append("results truncated", style="yellow bold")
        banner.append(
            f" — scanned {scan.events_scanned:,} events covering "
            f"{scan.describe_coverage()}; findings may be incomplete. "
            f"Narrow --since or raise --max-events."
        )
        console.print(banner, highlight=False)
        console.print()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_whoop_cli.py -k "truncation or scan_metadata or no_banner" -v`
Expected: PASS. Other `test_whoop_cli.py` tests will still fail at this point (the command has not been updated) — that is expected and fixed in Task 10; do **not** commit yet.

- [ ] **Step 5: Proceed directly to Task 10**

No commit here — Task 9 and Task 10 land together so the suite is green at the commit.

---

## Task 10: Wire `doctor` to `scan()` and add `--max-events`

**Files:**
- Modify: `src/prog_strength_tooling/commands/whoop.py`
- Modify: `src/prog_strength_tooling/whooplogs.py` (delete the old pair)
- Test: `tests/test_whoop_cli.py`
- Test: `tests/test_whooplogs.py` (delete the old pair's tests)

- [ ] **Step 1: Write the failing test**

In `tests/test_whoop_cli.py`, replace the `scans` fixture with:

```python
@pytest.fixture
def scans(monkeypatch):
    """Install a fixture WhoopScan for the doctor's log checks."""

    def install(deliveries: DeliveryScan, syncs: SyncScan, *, truncated: bool = False):
        result = _scan(deliveries, syncs, truncated=truncated)
        monkeypatch.setattr(whooplogs, "scan", lambda *a, **k: result)
        return result

    return install
```

In `test_doctor_cloudwatch_error_exits_2`, change the monkeypatch target:

```python
    monkeypatch.setattr(whooplogs, "scan", boom)
```

Then append these new tests:

```python
def test_doctor_forwards_max_events_to_the_scan(monkeypatch):
    seen = {}

    def capture(cfg, window, max_events):
        seen["max_events"] = max_events
        return _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS)

    monkeypatch.setattr(whooplogs, "scan", capture)
    result = runner.invoke(app, ["whoop", "doctor", "--max-events", "500"])
    assert result.exit_code == 0
    assert seen["max_events"] == 500


def test_doctor_defaults_to_the_module_cap(monkeypatch):
    seen = {}

    def capture(cfg, window, max_events):
        seen["max_events"] = max_events
        return _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS)

    monkeypatch.setattr(whooplogs, "scan", capture)
    runner.invoke(app, ["whoop", "doctor"])
    assert seen["max_events"] == whooplogs.MAX_EVENTS


def test_doctor_rejects_a_negative_max_events(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    result = runner.invoke(app, ["whoop", "doctor", "--max-events", "-1"])
    assert result.exit_code == 2
    assert "--max-events" in result.output


def test_doctor_banners_a_truncated_scan(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    result = runner.invoke(app, ["whoop", "doctor"])
    # Still healthy — truncation is a caveat on the evidence, not a finding.
    assert result.exit_code == 0
    assert "results truncated" in _plain(result.output)


def test_doctor_json_reports_truncation(scans):
    scans(OUTAGE_DELIVERIES, OUTAGE_SYNCS, truncated=True)
    result = runner.invoke(app, ["whoop", "doctor", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["scan"]["truncated"] is True
```

In `tests/test_whooplogs.py`, delete these six now-obsolete tests (their coverage is replaced by the `scan` tests added in Task 8): `test_outage_delivery_scan_groups_the_misroute`, `test_outage_sync_scan_sees_no_window_syncs`, `test_healthy_delivery_scan_groups_successful_posts`, `test_healthy_sync_scan_counts_window_syncs_and_sums_upserted`, and `test_scans_query_only_the_api_group_with_a_quoted_whoop_pattern`. Then rewrite the four remaining `scan_deliveries` callers to use `scan(...).deliveries`:

```python
def test_delivery_scan_skips_unparseable_lines(fake_client):
    fake_client(
        [
            _page(
                _event(1_000, "this is not a request line at all"),
                _event(1_500, '{"msg":"whoopsync: sync complete","kind":"window"}'),
                _event(2_000, _request_line("POST", "http://h/webhooks/whoop", 204)),
            )
        ]
    )
    result = whooplogs.scan(CFG, WINDOW).deliveries
    assert [(g.uri, g.status, g.count) for g in result.groups] == [("/webhooks/whoop", 204, 1)]


def test_delivery_scan_ignores_non_post_and_non_whoop(fake_client):
    fake_client(
        [
            _page(
                _event(1_000, _request_line("GET", "http://h/webhooks/whoop", 200)),
                _event(2_000, _request_line("POST", "http://h/webhooks/strava", 204)),
                _event(3_000, _request_line("POST", "http://h/webhooks/whoop", 204)),
            )
        ]
    )
    result = whooplogs.scan(CFG, WINDOW).deliveries
    assert [(g.uri, g.status, g.count) for g in result.groups] == [("/webhooks/whoop", 204, 1)]


def test_delivery_scan_matches_whoop_case_insensitively(fake_client):
    fake_client([_page(_event(1_000, _request_line("POST", "http://h/Webhooks/WHOOP", 204)))])
    assert whooplogs.scan(CFG, WINDOW).deliveries.groups[0].count == 1


def test_delivery_groups_sorted_by_count_desc(fake_client):
    events = [_event(i, _request_line("POST", "http://h/webhooks/whoop,", 404)) for i in range(3)]
    events += [
        _event(100 + i, _request_line("POST", "http://h/webhooks/whoop", 204)) for i in range(7)
    ]
    fake_client([_page(*events)])
    result = whooplogs.scan(CFG, WINDOW).deliveries
    assert [(g.status, g.count) for g in result.groups] == [(204, 7), (404, 3)]
```

Also add an outage-fixture test to replace the two deleted outage ones:

```python
def test_scan_of_the_outage_fixture_shows_the_misroute_and_no_syncs(fake_client):
    fake_client([_outage_page()])
    result = whooplogs.scan(CFG, WINDOW)
    assert [(g.uri, g.status, g.count) for g in result.deliveries.groups] == [
        ("/webhooks/whoop,", 404, 97)
    ]
    assert result.syncs.window_sync_count == 0
    assert result.syncs.upserted_total == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_whoop_cli.py -v`
Expected: FAIL — `AttributeError: <module 'prog_strength_tooling.whooplogs'> has no attribute 'scan'` is gone, but the doctor tests fail on `TypeError: render_diagnosis() missing 1 required positional argument: 'scan'` and `no such option: --max-events`.

- [ ] **Step 3: Write minimal implementation**

In `src/prog_strength_tooling/whooplogs.py`, **delete** the `scan_deliveries` and `scan_syncs` functions entirely. Leaving them as wrappers would let the double scan creep back in, and they have no callers outside this package.

In `src/prog_strength_tooling/commands/whoop.py`, add a `--max-events` option to `doctor` immediately after the `since` option:

```python
    max_events: int = typer.Option(
        whooplogs.MAX_EVENTS,
        "--max-events",
        help="Stop the CloudWatch scan after this many events; 0 scans the whole window.",
    ),
```

Add the validation immediately after the docstring, before the first `try:`:

```python
    if max_events < 0:
        err_console.print("[red]error:[/red] --max-events must be 0 or greater.")
        raise typer.Exit(code=EXIT_ERROR)
```

Replace the two scan calls in the first `try:` block:

```python
    try:
        # The log checks are non-negotiable, so their config/window errors are
        # fatal (exit 2) — same shape as logs.py.
        cfg_logs = resolve_logs(env, None, profile, region)
        window = resolve(since or DEFAULT_SINCE, None, None)
        scan = whooplogs.scan(cfg_logs, window, max_events)
    except (ConfigError, WindowError, cloudwatch.CloudWatchError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=EXIT_ERROR)
```

Update the diagnosis and render calls at the end of `doctor`:

```python
    diagnosis = whoop.diagnose(
        scan.deliveries, scan.syncs, connection, token_present, now=datetime.now(timezone.utc)
    )
    render_diagnosis(diagnosis, scan, as_json=as_json)
```

Update the module docstring's `doctor` paragraph to mention the single pass — replace the sentence beginning "The five log-derived checks" with:

```
`doctor` is designed to work with *whatever* credentials the operator has. The
five log-derived checks (deliveries arriving, on the served path, with an
accepted signature, producing syncs, landing rows) read CloudWatch with the
operator's own AWS creds — no admin token needed, and all five are served from
a single capped pass over the api log group (`whooplogs.scan`). The two
admin-derived checks (connection health, data freshness) need an admin JWT AND
a `--user`; when either is absent they degrade to *skipped* rather than
failing, so a missing `PST_TOKEN` never blocks the log diagnosis. This mirrors
how the original outage was actually found: from the api's request log alone.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q`
Expected: PASS — the entire suite is green.

- [ ] **Step 5: Commit**

```bash
uv run black . && uv run ruff check . && uv run pytest -q
git add src/prog_strength_tooling/whooplogs.py src/prog_strength_tooling/render.py \
        src/prog_strength_tooling/commands/whoop.py tests/test_whooplogs.py tests/test_whoop_cli.py
git commit -m "fix(whoop): scan the api log group once, capped, and flag truncation

doctor paginated the same filter_log_events query twice — once for
deliveries, once for syncs — over the same window with no MaxItems cap,
which is why it appeared to hang. One capped pass now feeds both, and a
truncated read is reported rather than presented as a complete diagnosis."
```

---

## Task 11: Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the logging row to the Settings table**

In `README.md`, in the `### Settings` table, add a row after the `AWS profile` row:

```markdown
| Log level | `-v` / `-vv` / `-q` | `PST_LOG_LEVEL` | `info` |
```

- [ ] **Step 2: Add a logging section**

Immediately after the `export PST_ENV=local` code fence that closes the Settings section, add:

````markdown
### Logging & diagnostics

Every command logs what it is doing to **stderr** — stdout stays clean, so
`--json | jq` is unaffected. The default is INFO: which environment and region
resolved, which log groups are being scanned, and how long each phase took.

```bash
pst whoop doctor            # INFO — phases and durations
pst -v whoop doctor         # DEBUG — config precedence, per-page paging, per-check results
pst -vv whoop doctor        # + botocore/httpx wire logs
pst -q whoop doctor         # warnings and errors only
export PST_LOG_LEVEL=debug  # same as -v, once per shell (an explicit flag wins)
```

When a command seems to hang, `-v` is the first thing to reach for: it shows
the CloudWatch page-by-page progress and the configured HTTP timeout, which
distinguishes "waiting out a 30s timeout" from "genuinely stuck". Admin tokens
are never logged at any level — only their last four characters and length.
````

- [ ] **Step 3: Add `logsetup.py` to the layout block**

In the `### Layout` code fence, add this line immediately after the `cli.py` line:

```
  logsetup.py         # log level resolution, stderr handler, timing helpers
```

and add after the `commands/logs.py` line:

```
  commands/whoop.py   # `pst whoop doctor` / `resync`
  whoop.py            # the doctor's diagnosis engine (pure)
  whooplogs.py        # one capped CloudWatch pass for the doctor's evidence
```

- [ ] **Step 4: Verify the full check suite**

Run: `uv run black --check . && uv run ruff check . && uv run pytest`
Expected: all three green, no test failures.

Then verify the CLI actually behaves, with no AWS call needed:

Run: `uv run pst --help`
Expected: help text shows `--verbose`, `-v`, `--quiet`, `-q`, and the epilog mentions `PST_LOG_LEVEL`.

Run: `uv run pst whoop doctor --help`
Expected: help text shows `--max-events`.

Run: `uv run pst -v status --env local`
Expected: DEBUG lines on stderr showing `[prog_strength_tooling.config]` environment resolution and `[prog_strength_tooling.health]` probe lines, then the usual status table on stdout (services will be down unless a local stack is running — that is fine, the point is the log output).

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: document -v/-vv/-q, PST_LOG_LEVEL, and the whoop scan cap"
```

---

## Task 12: Open the PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/cli-logging
```

- [ ] **Step 2: Open the PR**

The PR **title** drives the release (`feat` → minor bump), so it must be a Conventional Commit:

```bash
gh pr create --base main --title "feat(logging): add info/debug logging and fix whoop doctor's double scan" --body "$(cat <<'EOF'
## What

`pst` had no logging at all, so a command that spent minutes inside a
CloudWatch paginator looked identical to one that had deadlocked. This adds
INFO-level phase/duration logging by default and DEBUG diagnostics on demand,
then fixes the specific hang that motivated it.

- New `logsetup.py`: level resolution (`-v` / `-vv` / `-q`, `PST_LOG_LEVEL`),
  a stderr `RichHandler`, and `timed` / `Heartbeat` helpers.
- Root `@app.callback()` carries the verbosity flags for every subcommand.
- Instrumentation across config, window, cloudwatch, client, health, and the
  doctor's check engine. Logs are stderr-only, so `--json` stays pipeable, and
  admin tokens are never logged at any level.
- `whoop doctor` paginated the same `filter_log_events` query **twice** (once
  for deliveries, once for syncs) over the same window with **no `MaxItems`
  cap**. One capped pass (`whooplogs.scan`, `--max-events`, default 20k) now
  feeds both, and a truncated read is reported with the timestamp range it
  actually covered rather than presented as a complete diagnosis.

## Verification

`uv run black --check .`, `uv run ruff check .`, and `uv run pytest` all green.

Design: `docs/superpowers/specs/2026-07-31-cli-logging-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Notes for the implementer

- **Never log a token.** `config.py`, `client.py`, and the `cli.py` startup line all pass through `logsetup.redact` / `redact_argv`. Two tests assert a secret never reaches a log record; if you add a new log line near a token, add it to those assertions.
- **INFO is phases, not results.** If a table already prints something, it does not belong at INFO. `health.py` is the worked example — its probes log at DEBUG because `pst status` already renders latency.
- **stderr only.** `render.py`'s `console` is stdout and is for results; `logsetup`'s handler is stderr and is for progress. Do not mix them.
- **Coverage is measured.** `WhoopScan.covered_start` / `covered_end` come from real event timestamps. Do not "simplify" them to the requested window bounds — a capped scan keeps the oldest slice, so the two genuinely differ.
