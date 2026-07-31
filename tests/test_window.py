"""Time-window resolution: --since parsing and --start/--end handling."""

from datetime import UTC, datetime, timedelta

import pytest

from prog_strength_tooling.window import WindowError, parse_duration, resolve

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("30s", timedelta(seconds=30)),
        ("90m", timedelta(minutes=90)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("1.5h", timedelta(hours=1.5)),
        ("24H", timedelta(hours=24)),
        (" 24h ", timedelta(hours=24)),
    ],
)
def test_parse_duration_accepts_supported_specs(spec, expected):
    assert parse_duration(spec) == expected


@pytest.mark.parametrize("spec", ["24", "24hh", "h", "-1h", "0h", "1w", "abc", ""])
def test_parse_duration_rejects_bad_specs(spec):
    with pytest.raises(WindowError):
        parse_duration(spec)


def test_defaults_to_last_24h():
    window = resolve(None, None, None, now=NOW)
    assert window.end == NOW
    assert window.start == NOW - timedelta(hours=24)
    assert window.since == "24h"
    assert "last 24h" in window.describe()


def test_since_sets_the_window_relative_to_now():
    window = resolve("7d", None, None, now=NOW)
    assert window.start == NOW - timedelta(days=7)


def test_since_with_explicit_bounds_is_rejected():
    # Silently picking a winner here would change what the query can find.
    with pytest.raises(WindowError, match="cannot be combined"):
        resolve("7d", datetime(2026, 7, 29, tzinfo=UTC), None, now=NOW)


def test_explicit_bounds_are_used_verbatim():
    start = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    end = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    window = resolve(None, start, end, now=NOW)
    assert (window.start, window.end) == (start, end)
    assert window.since is None
    assert "2026-07-29 10:00Z" in window.describe()


def test_start_alone_runs_through_to_now():
    start = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    assert resolve(None, start, None, now=NOW).end == NOW


def test_naive_timestamps_are_read_as_utc_not_local():
    # Everything this CLI prints is UTC; reading input as local time would be
    # an off-by-hours that quietly excludes the lines you're looking for.
    window = resolve(None, datetime(2026, 7, 29, 10, 0), None, now=NOW)
    assert window.start == datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


def test_inverted_bounds_are_rejected():
    with pytest.raises(WindowError, match="earlier than"):
        resolve(
            None,
            datetime(2026, 7, 30, tzinfo=UTC),
            datetime(2026, 7, 29, tzinfo=UTC),
            now=NOW,
        )


def test_epoch_millisecond_conversion():
    window = resolve(None, datetime(2026, 7, 29, tzinfo=UTC), NOW, now=NOW)
    assert window.start_ms == int(datetime(2026, 7, 29, tzinfo=UTC).timestamp() * 1000)
    assert window.end_ms == int(NOW.timestamp() * 1000)


def test_resolve_logs_the_relative_window_at_info(caplog):
    # Despite the old name of this test, positional args are (since, start,
    # end) -- "7d" here is --since, so this exercises the RELATIVE branch,
    # not the explicit --start/--end one. See the explicit-bounds test below.
    import logging

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        resolve("7d", None, None)

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "since=7d" in line
    assert "start=" in line
    assert "end=" in line


def test_resolve_logs_the_explicit_window_at_info(caplog):
    import logging

    start = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
    end = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        resolve(None, start, end)

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "start=" in line
    assert "end=" in line
    # This branch logs since=None, and kv() drops None values -- so "since="
    # must be ABSENT here. That absence, not just the presence of start=/
    # end=, is what actually distinguishes this branch from the relative one
    # above (which also logs start=/end=, derived from the duration).
    assert "since=" not in line
