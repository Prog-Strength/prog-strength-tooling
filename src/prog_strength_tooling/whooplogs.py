"""WHOOP delivery + sync evidence, scanned from the api's CloudWatch logs.

`scan` reads the api log group ONCE over one time window and folds every
matching event into both of the aggregations the WHOOP integration doctor's
checks consume:

  * deliveries — every request the api logged for a POST whose path mentions
    "whoop", grouped by (uri, status) with a count. A provider posting to the
    wrong path (say a stray trailing comma, "/webhooks/whoop,") misses the
    `r.Post("/webhooks/whoop", …)` route and 404s: it shows up here as
    (uri="/webhooks/whoop,", status=404, count=N) instead of a healthy
    (uri="/webhooks/whoop", status=204, …).

  * syncs — the `whoopsync: sync complete` lines the api service emits after
    each sync. Counts the kind="window" ones and sums their `upserted`,
    because "the recent-window sync ran but upserted zero rows" is the shape
    of a silently-empty dashboard.

One pass, not two: both aggregations key off the same '"whoop"' filter over
the same window, so running them as separate paginations doubled the AWS
calls and doubled the wait for no new information. Worse, neither pass had a
ceiling — an uncapped paginator over a 7-day window of a busy log group is
how `doctor` came to look like it had hung. `scan` caps at MAX_EVENTS and
reports what it actually managed to read (see WhoopScan).

`scan` is the only entry point, deliberately: the two functions it replaced
(one per aggregation) were what made the double pagination possible, and
keeping them as thin wrappers would have left that mistake one call site away
from returning. A new aggregation belongs in the fold loop inside `scan`, not
in a second pass over the same events.

Why one FilterLogEvents call + client-side aggregation (not Logs Insights):
same rationale cloudwatch.py documents — FilterLogEvents is billed per call
rather than per-GB-scanned, returns synchronously (no start/poll/get-results
loop), and a quoted filterPattern is a literal substring match. We pull the
"whoop" lines and aggregate here; the counts are small and the alternative
(Insights `stats … by …`) buys nothing for a handful of groupings.

AWS setup and error handling are NOT reimplemented here — build_client and
describe_failure/CloudWatchError are reused from cloudwatch so "no creds / no
permission / wrong region" reads identically to `pst logs`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from . import cloudwatch
from .config import LogsConfig, log_group_for
from .logsetup import Heartbeat, get_logger, kv, timed
from .window import Window

log = get_logger(__name__)

#: Message string the api logs on a completed sync (internal/whoopsync/
#: service.go). Matched exactly so an unrelated line that merely contains
#: "whoop" (an OAuth log, a request line) is never miscounted as a sync.
SYNC_COMPLETE_MSG = "whoopsync: sync complete"

#: The "recent window" sync kind (service.go calls syncWindow with "window";
#: the others are "backfill" and "admin_resync"). Only window syncs answer
#: "is fresh data still landing on its own?", so it's the one the scan totals.
WINDOW_KIND = "window"

#: A chi middleware.Logger request line as it lands in CloudWatch. chi writes
#: it through Go's stdlib `log` with LstdFlags, so it is NOT JSON — logparse's
#: JSON reader skips it. Shape (see go-chi/chi middleware/logger.go):
#:   2026/07/30 12:00:00 "POST http://host/uri HTTP/1.1" from ADDR - 404 19B in …
#: We capture method, the full request URL, and the status code. The date
#: prefix is ignored (CloudWatch's own event timestamp is authoritative).
_REQUEST_RE = re.compile(
    r'"(?P<method>[A-Z]+)\s+(?P<url>\S+)\s+HTTP/[\d.]+"'  # the quoted request line
    r".*?-\s+(?P<status>\d{3})\b"  # " - <status>" written by defaultLogEntry.Write
)


@dataclass(frozen=True)
class DeliveryGroup:
    """One (uri, status) bucket of whoop POST requests, with its count."""

    uri: str
    status: int
    count: int


@dataclass(frozen=True)
class DeliveryScan:
    """All whoop POST delivery buckets over the window, sorted deterministically."""

    groups: list[DeliveryGroup]


@dataclass(frozen=True)
class SyncScan:
    """Aggregate of the window sync-complete lines over the window."""

    window_sync_count: int
    upserted_total: int


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
        return f"{self.covered_start:%Y-%m-%d %H:%M}Z to {self.covered_end:%Y-%m-%d %H:%M}Z"


def _paginate_whoop(client, group: str, window: Window, max_events: int):
    """Yield each page of "whoop" events in the api log group over the window.

    Yields whole pages rather than individual events so the caller can count
    pages for its progress heartbeat and stop mid-page when the cap trips.

    A single literal-substring filterPattern ('"whoop"') server-side, matching
    cloudwatch.py's quoted-pattern convention and its window -> startTime/
    endTime handling (epoch millis). Both request lines and sync lines carry
    the substring, so one pass feeds both aggregations.

    `max_events` (0 = unlimited) is passed as MaxItems so botocore stops
    fetching server-side too, mirroring cloudwatch._search_group. The `+ 1` is
    not slack: it is what makes the boundary decidable. Fetching one event
    beyond the quota lets the caller distinguish "that was everything" from
    "we cut it short" — without it, a window holding precisely `max_events`
    events is indistinguishable from one holding more, and would be reported
    as truncated when nothing was actually dropped. The caller still counts
    client-side, because that extra event is evidence to be observed, not data
    to be folded into the results.
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


def _uri_path(url: str) -> str:
    """Path portion of a chi request URL.

    chi logs the full RequestURI as scheme://host/path (see logger.go), but the
    doctor groups on the path — that is where the trailing-comma misroute is
    visible and it does not vary with host. A bare path (no scheme) is returned
    unchanged, so a future non-absolute log form still groups sensibly.
    """
    after_scheme = url.split("://", 1)[-1]
    slash = after_scheme.find("/")
    return after_scheme[slash:] if slash != -1 else after_scheme


def _parse_sync(message: str) -> dict | None:
    """Decode a `whoopsync: sync complete` JSON slog line, or None.

    None for anything that isn't that exact message: a request line, an OAuth
    log, or malformed JSON. Guards mean a surprise line can never crash a scan.
    """
    stripped = message.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("msg") != SYNC_COMPLETE_MSG:
        return None
    return payload


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


def _as_dt(stamp_ms: int | None) -> datetime | None:
    """CloudWatch epoch-millis to an aware UTC datetime, or None."""
    return None if stamp_ms is None else datetime.fromtimestamp(stamp_ms / 1000, tz=UTC)


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
                    if max_events and events_scanned >= max_events:
                        # We already have our full quota, and yet here is
                        # another event: that is the proof the window held more
                        # than we read. Checking BEFORE processing (rather than
                        # after) is what makes `truncated` honest at the exact
                        # boundary — a window holding precisely `max_events`
                        # events was read in full, not cut short, and must not
                        # warn. MaxItems is max_events + 1 for exactly this
                        # reason: it buys the one extra event whose existence
                        # answers the question. This event is deliberately not
                        # folded in, so the counts and the covered range
                        # describe only what we actually read.
                        truncated = True
                        break

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
        # Phrased to match render.render_diagnosis's operator banner: same
        # event, so it must not offer different advice. Both name the OLDEST
        # slice explicitly and both recommend narrowing --since or --max-events
        # 0 — never "raise --max-events", which reads further forward from the
        # same oldest starting point and may still never reach recent data.
        log.warning(
            "scan hit --max-events %d after %d pages; covered the OLDEST %s of the "
            "requested %s. CloudWatch returns events oldest-first, so more recent "
            "events were not read. Narrow --since, or pass --max-events 0 to scan "
            "the whole window.",
            max_events,
            pages,
            result.describe_coverage(),
            window.describe(),
        )

    return result
