"""Time-window resolution for log queries.

CloudWatch's FilterLogEvents takes an explicit start/end, so every log query
has to turn operator input into a concrete window. Two ways to express one:
a relative `--since 24h`, or an absolute `--start`/`--end` pair.

Pure functions with an injectable `now`, so the tests don't have to freeze the
clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: Duration suffixes accepted by --since, in seconds.
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd])$", re.IGNORECASE)

#: What prog-strength-infra sets on the log groups (modules/logging:
#: retention_days). Used only for guidance text — CloudWatch enforces the real
#: cutoff, we just avoid suggesting a window that can't contain anything.
RETENTION_DAYS = 30


class WindowError(ValueError):
    """Invalid or contradictory time-window input."""


@dataclass(frozen=True)
class Window:
    """A resolved, absolute query window (both bounds UTC)."""

    start: datetime
    end: datetime
    #: The relative spec that produced it ("24h"), or None for explicit bounds.
    since: str | None = None

    def describe(self) -> str:
        """Human phrasing for headers and the empty-result hint."""
        if self.since:
            return f"last {self.since}"
        return f"{self.start:%Y-%m-%d %H:%M}Z to {self.end:%Y-%m-%d %H:%M}Z"

    @property
    def start_ms(self) -> int:
        """Start as epoch milliseconds, the unit the CloudWatch API takes."""
        return int(self.start.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end.timestamp() * 1000)


def parse_duration(spec: str) -> timedelta:
    """Parse a `--since` value like `90m`, `24h`, `7d` into a timedelta.

    Deliberately strict: a typo like `24hh` or a bare `24` is an error rather
    than a guess, because guessing wrong here silently changes what the query
    can find.
    """
    match = _DURATION_RE.match(spec.strip())
    if not match:
        raise WindowError(
            f"invalid duration {spec!r}. Use a number followed by s, m, h, or d — e.g. 90m, 24h, 7d."
        )
    value = float(match.group("value"))
    if value <= 0:
        raise WindowError(f"invalid duration {spec!r}: must be greater than zero.")
    return timedelta(seconds=value * _UNITS[match.group("unit").lower()])


def _as_utc(value: datetime) -> datetime:
    """Normalize an operator-supplied timestamp to UTC.

    A naive timestamp is read as UTC rather than local time: the log lines and
    every other timestamp this CLI prints are UTC, so interpreting one input
    differently is the kind of off-by-hours that costs an investigation.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve(
    since: str | None,
    start: datetime | None,
    end: datetime | None,
    *,
    now: datetime | None = None,
) -> Window:
    """Resolve operator input into an absolute window.

    `--since` and `--start`/`--end` are mutually exclusive: accepting both
    would mean silently picking a winner. `--end` alone defaults its start to
    the window implied by `since`; `--start` alone runs through to now.
    """
    now = now or datetime.now(UTC)

    if since and (start or end):
        raise WindowError("--since cannot be combined with --start/--end. Use one or the other.")

    if start or end:
        resolved_end = _as_utc(end) if end else now
        resolved_start = _as_utc(start) if start else resolved_end - timedelta(hours=24)
        if resolved_start >= resolved_end:
            raise WindowError("--start must be earlier than --end.")
        return Window(start=resolved_start, end=resolved_end, since=None)

    spec = since or "24h"
    return Window(start=now - parse_duration(spec), end=now, since=spec)
