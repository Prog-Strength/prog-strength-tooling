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
        # --quiet beats -v/-vv when both are given: silence wins.
        (2, True, None, logging.WARNING, False),
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


# --- field rendering and redaction ----------------------------------------


def test_kv_renders_greppable_pairs_and_drops_none():
    assert logsetup.kv(group="/prog-strength/api", pages=3, empty=None) == (
        "group=/prog-strength/api pages=3"
    )


def test_kv_with_no_fields_is_empty():
    assert logsetup.kv() == ""


def test_kv_keeps_falsy_but_not_none_values():
    # Insurance against "simplifying" the None-check to a truthiness check.
    assert logsetup.kv(pages=0, name="") == "pages=0 name="


def test_redact_shows_only_the_tail_and_length():
    token = "eyJhbGciOiJIUzI1NiJ9.payload.sig9"
    out = logsetup.redact(token)
    assert out == f"…sig9 (len {len(token)})"
    assert "eyJhbGci" not in out


def test_redact_hides_short_tokens_entirely():
    # Too short for a 4-char tail to be safe — show nothing but the length.
    assert logsetup.redact("abc123") == "present (len 6)"


def test_redact_at_the_min_redactable_len_boundary():
    # Pins the `<` vs `<=` choice: 11 chars stays hidden, 12 gets a tail.
    assert logsetup.redact("a" * 11) == "present (len 11)"
    assert logsetup.redact("a" * 12) == "…aaaa (len 12)"


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


def test_timed_logs_a_debug_start_line_on_entry(caplog):
    log = logging.getLogger("prog_strength_tooling.test")
    with caplog.at_level(logging.DEBUG, logger="prog_strength_tooling.test"):
        with logsetup.timed(log, "whoop log scan", group="/prog-strength/api"):
            pass

    debug_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug_messages) == 1
    assert "whoop log scan" in debug_messages[0]


def test_timed_key_collision_in_finally_does_not_mask_the_bodys_exception(caplog):
    """A `finally` that raises would replace the real exception with a
    spurious TypeError — exactly the failure mode this helper must avoid."""
    log = logging.getLogger("prog_strength_tooling.test")
    with caplog.at_level(logging.INFO, logger="prog_strength_tooling.test"):
        with pytest.raises(ValueError):
            with logsetup.timed(log, "whoop log scan", pages=1) as extra:
                extra["pages"] = 2  # collides with the `fields` key above
                raise ValueError("boom")


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
