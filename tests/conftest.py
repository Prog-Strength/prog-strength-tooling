"""Shared test fixtures.

The suite must never read the operator's own `PST_*` environment. `config.py`
resolves flags, then env vars, then defaults — so an exported `PST_TOKEN` in the
developer's shell silently satisfies a test that was asserting the *missing*
token path, and the command then proceeds to make a real request against the
configured environment (which defaults to **prod**). That is how
`test_resync_missing_token_exits_nonzero` came to fail on a clean checkout with
a live 404 from the production API rather than the expected error panel.

Clearing the whole `PST_*` namespace here, for every test, makes the suite
depend only on what each test sets explicitly. Tests that want a value pass it
through `runner.invoke(env=...)` or `monkeypatch.setenv`, both of which layer on
top of this cleared baseline.
"""

from __future__ import annotations

import logging
import os

import pytest

from prog_strength_tooling import logsetup

#: Prefix covering every environment variable this CLI reads (PST_ENV,
#: PST_TOKEN, PST_API_URL, PST_AWS_PROFILE, PST_LOG_LEVEL). Matched by prefix
#: rather than listed, so a future variable is isolated the day it is added.
PST_PREFIX = "PST_"


@pytest.fixture(autouse=True)
def _isolate_pst_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every PST_* variable so the operator's shell can't reach a test."""
    for name in [key for key in os.environ if key.startswith(PST_PREFIX)]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _restore_logging():
    """Undo whatever configure() did, so tests don't leak levels into each other.

    `logsetup.configure()` mutates process-global logging state: the root
    logger's handlers and level, the package logger's level, and every logger
    in `WIRE_LOGGERS`. Without restoring all of that after each test, whichever
    test last called configure() (directly, or indirectly via `runner.invoke`
    hitting the root Typer callback) silently dictates the logging levels every
    later test observes — a real, order-dependent flake, not a theoretical one.
    Autouse and hoisted into conftest so every test module gets this for free,
    not just the ones that happen to remember to ask for it.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    pst = logging.getLogger(logsetup.ROOT_LOGGER)
    saved_pst_level = pst.level
    saved_wire_levels = {name: logging.getLogger(name).level for name in logsetup.WIRE_LOGGERS}
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    pst.setLevel(saved_pst_level)
    for name, level in saved_wire_levels.items():
        logging.getLogger(name).setLevel(level)
