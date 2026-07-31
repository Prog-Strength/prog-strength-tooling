"""whooplogs scans, stubbed at the botocore/paginator layer.

Same approach as test_cloudwatch.py: a fake paginator monkeypatched onto a
fake client returned by cloudwatch.build_client, yielding canned pages so no
network or credentials are needed. The log lines below are the REAL shapes the
api emits:

  * request lines come from chi's stock middleware.Logger
    (internal/server/server.go: `r.Use(middleware.Logger)`), which writes a
    Go stdlib `log` line — LstdFlags date prefix + chi's
    `"METHOD scheme://host/uri PROTO" from ADDR - STATUS BYTESB in ELAPSED`.
    A webhook posted to the wrong path (`/webhooks/whoop,`) misses the
    `r.Post("/webhooks/whoop", …)` route and 404s via the NotFound handler.
  * sync lines come from `slog.InfoContext(ctx, "whoopsync: sync complete",
    "kind", …, "upserted", …, …)` (internal/whoopsync/service.go), rendered
    by the JSON slog handler in internal/logging.
"""

import json

import pytest

from prog_strength_tooling import cloudwatch, whooplogs
from prog_strength_tooling.config import LogsConfig
from prog_strength_tooling.window import resolve

WINDOW = resolve("24h", None, None)

CFG = LogsConfig(
    environment="prod",
    region="us-east-2",
    profile=None,
    log_groups={
        "api": "/prog-strength/api",
        "agent": "/prog-strength/agent",
        "mcp": "/prog-strength/mcp",
    },
)


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return list(self._pages)


class _FakeClient:
    def __init__(self, pages):
        self.paginator = _FakePaginator(pages)

    def get_paginator(self, _name):
        return self.paginator


@pytest.fixture
def fake_client(monkeypatch):
    """Install a fake client whose filter_log_events paginator yields pages."""

    def install(pages):
        client = _FakeClient(pages)
        monkeypatch.setattr(cloudwatch, "build_client", lambda cfg: client)
        return client

    return install


def _event(ts_ms, message, stream="container-abc"):
    return {"timestamp": ts_ms, "message": message, "logStreamName": stream}


def _page(*events):
    return {"events": list(events), "searchedLogStreams": []}


def _request_line(method, url, status, ts="2026/07/30 12:00:00"):
    """A chi middleware.Logger request line, exactly as it lands in CloudWatch."""
    return f'{ts} "{method} {url} HTTP/1.1" from 10.0.0.5:34512 - {status} 19B in 214.6µs'


def _sync_line(kind, upserted, msg="whoopsync: sync complete"):
    """A `whoopsync: sync complete` JSON slog line from the api service."""
    return json.dumps(
        {
            "time": "2026-07-30T12:00:00Z",
            "level": "INFO",
            "msg": msg,
            "kind": kind,
            "user_id": "user-123",
            "window_start": "2026-07-29T12:00:00Z",
            "window_end": "2026-07-30T12:00:00Z",
            "recoveries_fetched": upserted,
            "cycles_fetched": upserted,
            "upserted": upserted,
            "skipped_unscored": 0,
            "skipped_no_cycle": 0,
            "skipped_bad_date": 0,
            "request_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        }
    )


# --- outage fixture: misdelivered webhooks + no window syncs --------------


def _outage_page():
    misdeliver = "http://api.progstrength.fitness/webhooks/whoop,"
    events = [_event(1_000 + i, _request_line("POST", misdeliver, 404)) for i in range(97)]
    return _page(*events)


def test_outage_delivery_scan_groups_the_misroute(fake_client):
    fake_client([_outage_page()])
    scan = whooplogs.scan_deliveries(CFG, WINDOW)
    assert len(scan.groups) == 1
    group = scan.groups[0]
    assert group.uri == "/webhooks/whoop,"
    assert group.status == 404
    assert group.count == 97


def test_outage_sync_scan_sees_no_window_syncs(fake_client):
    fake_client([_outage_page()])
    scan = whooplogs.scan_syncs(CFG, WINDOW)
    assert scan.window_sync_count == 0
    assert scan.upserted_total == 0


# --- healthy fixture: correct deliveries + window syncs -------------------


def _healthy_page():
    good = "http://api.progstrength.fitness/webhooks/whoop"
    events = [_event(1_000 + i, _request_line("POST", good, 204)) for i in range(5)]
    events += [
        _event(5_000, _sync_line("window", 3)),
        _event(6_000, _sync_line("window", 2)),
        # A non-window sync must not be counted toward the window totals.
        _event(7_000, _sync_line("backfill", 40)),
    ]
    return _page(*events)


def test_healthy_delivery_scan_groups_successful_posts(fake_client):
    fake_client([_healthy_page()])
    scan = whooplogs.scan_deliveries(CFG, WINDOW)
    assert len(scan.groups) == 1
    group = scan.groups[0]
    assert group.uri == "/webhooks/whoop"
    assert group.status == 204
    assert group.count == 5


def test_healthy_sync_scan_counts_window_syncs_and_sums_upserted(fake_client):
    fake_client([_healthy_page()])
    scan = whooplogs.scan_syncs(CFG, WINDOW)
    assert scan.window_sync_count == 2
    assert scan.upserted_total == 5


# --- shared behaviour -----------------------------------------------------


def test_scans_query_only_the_api_group_with_a_quoted_whoop_pattern(fake_client):
    client = fake_client([_page()])
    whooplogs.scan_deliveries(CFG, WINDOW)
    call = client.paginator.calls[0]
    assert call["logGroupName"] == "/prog-strength/api"
    assert call["filterPattern"] == '"whoop"'
    assert call["startTime"] == WINDOW.start_ms
    assert call["endTime"] == WINDOW.end_ms


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
    scan = whooplogs.scan_deliveries(CFG, WINDOW)
    assert [(g.uri, g.status, g.count) for g in scan.groups] == [("/webhooks/whoop", 204, 1)]


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
    scan = whooplogs.scan_deliveries(CFG, WINDOW)
    assert [(g.uri, g.status, g.count) for g in scan.groups] == [("/webhooks/whoop", 204, 1)]


def test_delivery_scan_matches_whoop_case_insensitively(fake_client):
    fake_client([_page(_event(1_000, _request_line("POST", "http://h/Webhooks/WHOOP", 204)))])
    scan = whooplogs.scan_deliveries(CFG, WINDOW)
    assert scan.groups[0].count == 1


def test_delivery_groups_sorted_by_count_desc(fake_client):
    events = [_event(i, _request_line("POST", "http://h/webhooks/whoop,", 404)) for i in range(3)]
    events += [
        _event(100 + i, _request_line("POST", "http://h/webhooks/whoop", 204)) for i in range(7)
    ]
    fake_client([_page(*events)])
    scan = whooplogs.scan_deliveries(CFG, WINDOW)
    assert [(g.status, g.count) for g in scan.groups] == [(204, 7), (404, 3)]


def test_scan_wraps_botocore_failure_as_cloudwatch_error(monkeypatch):
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "FilterLogEvents"
    )

    class Boom:
        def get_paginator(self, _name):
            class P:
                def paginate(self, **kwargs):
                    raise err

            return P()

    monkeypatch.setattr(cloudwatch, "build_client", lambda cfg: Boom())
    with pytest.raises(cloudwatch.CloudWatchError, match="logs:FilterLogEvents"):
        whooplogs.scan_deliveries(CFG, WINDOW)


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


def test_scan_of_the_outage_fixture_shows_the_misroute_and_no_syncs(fake_client):
    fake_client([_outage_page()])
    result = whooplogs.scan(CFG, WINDOW)
    assert [(g.uri, g.status, g.count) for g in result.deliveries.groups] == [
        ("/webhooks/whoop,", 404, 97)
    ]
    assert result.syncs.window_sync_count == 0
    assert result.syncs.upserted_total == 0
