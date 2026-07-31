"""WHOOP delivery + sync evidence, scanned from the api's CloudWatch logs.

Two scans that the WHOOP integration doctor's checks consume, both over one
time window against the single api log group:

  * scan_deliveries — every request the api logged for a POST whose path
    mentions "whoop", grouped by (uri, status) with a count. A provider
    posting to the wrong path (say a stray trailing comma, "/webhooks/whoop,")
    misses the `r.Post("/webhooks/whoop", …)` route and 404s: it shows up here
    as (uri="/webhooks/whoop,", status=404, count=N) instead of a healthy
    (uri="/webhooks/whoop", status=204, …).

  * scan_syncs — the `whoopsync: sync complete` lines the api service emits
    after each sync. Counts the kind="window" ones and sums their `upserted`,
    because "the recent-window sync ran but upserted zero rows" is the shape
    of a silently-empty dashboard.

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

from botocore.exceptions import BotoCoreError, ClientError

from . import cloudwatch
from .config import LogsConfig, log_group_for
from .window import Window

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


def _paginate_whoop(client, group: str, window: Window):
    """Yield every "whoop" event in the api log group over the window.

    A single literal-substring filterPattern ('"whoop"') server-side, matching
    cloudwatch.py's quoted-pattern convention and its window -> startTime/
    endTime handling (epoch millis). Both request lines and sync lines carry
    the substring, so one pass feeds both scans' callers.
    """
    paginator = client.get_paginator("filter_log_events")
    pages = paginator.paginate(
        logGroupName=group,
        startTime=window.start_ms,
        endTime=window.end_ms,
        filterPattern='"whoop"',
    )
    for page in pages:
        yield from page.get("events", [])


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


def scan_deliveries(cfg: LogsConfig, window: Window) -> DeliveryScan:
    """Group whoop POST request lines by (uri, status) with counts.

    Only POSTs whose path contains "whoop" (case-insensitive) count — a GET
    health probe or a strava webhook that happened to share the page is not a
    whoop delivery. Lines that don't parse as a request line are skipped
    (a sync line, an OAuth log): dropping them is right, not an error, since
    the "whoop" filter deliberately pulls in more than request lines.
    """
    client = cloudwatch.build_client(cfg)
    group = log_group_for("api")

    counts: dict[tuple[str, int], int] = {}
    try:
        for event in _paginate_whoop(client, group, window):
            match = _REQUEST_RE.search(event.get("message", ""))
            if match is None:
                continue
            if match.group("method") != "POST":
                continue
            uri = _uri_path(match.group("url"))
            if "whoop" not in uri.lower():
                continue
            status = int(match.group("status"))
            key = (uri, status)
            counts[key] = counts.get(key, 0) + 1
    except (ClientError, BotoCoreError) as exc:
        # Reuse the shared translation so credential/permission/region errors
        # read the same here as in `pst logs`.
        raise cloudwatch.CloudWatchError(cloudwatch.describe_failure(exc, group, cfg)) from exc

    # Deterministic order: busiest bucket first, then uri for a stable tie-break
    # so repeated runs and test assertions don't depend on dict/insertion order.
    groups = [
        DeliveryGroup(uri=uri, status=status, count=count)
        for (uri, status), count in counts.items()
    ]
    groups.sort(key=lambda g: (-g.count, g.uri, g.status))
    return DeliveryScan(groups=groups)


def scan_syncs(cfg: LogsConfig, window: Window) -> SyncScan:
    """Count kind="window" sync-complete lines and sum their upserted rows.

    Parses the JSON slog line directly for `kind`/`upserted` rather than via
    logparse: logparse flattens slog attrs into a display string, but these two
    fields need to stay typed (an int sum, an exact kind match). Non-JSON lines
    and JSON without the exact sync-complete message are skipped.
    """
    client = cloudwatch.build_client(cfg)
    group = log_group_for("api")

    window_sync_count = 0
    upserted_total = 0
    try:
        for event in _paginate_whoop(client, group, window):
            record = _parse_sync(event.get("message", ""))
            if record is None:
                continue
            if record.get("kind") != WINDOW_KIND:
                continue
            window_sync_count += 1
            upserted = record.get("upserted", 0)
            if isinstance(upserted, int):
                upserted_total += upserted
    except (ClientError, BotoCoreError) as exc:
        raise cloudwatch.CloudWatchError(cloudwatch.describe_failure(exc, group, cfg)) from exc

    return SyncScan(window_sync_count=window_sync_count, upserted_total=upserted_total)


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
