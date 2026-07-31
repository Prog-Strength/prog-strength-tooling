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
