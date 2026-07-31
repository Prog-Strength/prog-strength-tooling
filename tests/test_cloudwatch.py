"""CloudWatch query behaviour, stubbed at the botocore layer.

botocore's Stubber is the boto3 analogue of the respx mocking used for the
HTTP commands: canned responses, no network, no credentials required.
"""

import json

import boto3
import pytest
from botocore.stub import Stubber

from prog_strength_tooling import cloudwatch
from prog_strength_tooling.config import LogsConfig
from prog_strength_tooling.window import resolve

RID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
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


@pytest.fixture
def stubbed(monkeypatch):
    """A logs client whose calls are stubbed, wired into cloudwatch.search.

    Stubber hands out queued responses in call order, which is only
    deterministic when exactly one group is queried — the real search fans out
    across threads. Multi-group tests use `fake_client` instead.
    """
    client = boto3.client(
        "logs",
        region_name="us-east-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    stubber = Stubber(client)
    stubber.activate()
    monkeypatch.setattr(cloudwatch, "build_client", lambda cfg: client)
    yield stubber
    stubber.deactivate()


class _FakePaginator:
    """Returns canned pages per log group, so concurrent fan-out is stable."""

    def __init__(self, pages_by_group):
        self._pages_by_group = pages_by_group
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return self._pages_by_group.get(kwargs["logGroupName"], [_page()])


class _FakeClient:
    def __init__(self, pages_by_group):
        self.paginator = _FakePaginator(pages_by_group)

    def get_paginator(self, _name):
        return self.paginator


@pytest.fixture
def fake_client(monkeypatch):
    """Install a deterministic per-group client; returns the installer."""

    def install(pages_by_group):
        client = _FakeClient(pages_by_group)
        monkeypatch.setattr(cloudwatch, "build_client", lambda cfg: client)
        return client

    return install


def _event(ts_ms, message, stream="container-abc"):
    return {"timestamp": ts_ms, "message": message, "logStreamName": stream}


def _page(*events):
    return {"events": list(events), "searchedLogStreams": []}


def _json_line(msg, level="INFO"):
    return json.dumps(
        {"time": "2026-07-30T12:00:00Z", "level": level, "msg": msg, "request_id": RID}
    )


# --- request id validation ------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (RID, RID),
        (f"  {RID}  ", RID),
        (f'"{RID}"', RID),
        (f"'{RID}'", RID),
        # Not 32-hex: the api's middleware adopts any inbound X-Request-ID, so
        # a client- or proxy-supplied id is legitimate and must be searchable.
        ("trace-from-edge-abc123", "trace-from-edge-abc123"),
    ],
)
def test_validate_request_id_normalizes(raw, expected):
    assert cloudwatch.validate_request_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", 'has"quote', "has space", "has\nnewline", "a" * 300])
def test_validate_request_id_rejects_unusable(raw):
    with pytest.raises(cloudwatch.InvalidRequestIDError):
        cloudwatch.validate_request_id(raw)


# --- search ---------------------------------------------------------------


def test_search_merges_groups_into_one_time_ordered_timeline(fake_client):
    # api's hit is later than agent's; the merged timeline must interleave by
    # timestamp, not by which group was queried first.
    fake_client(
        {
            "/prog-strength/api": [_page(_event(2_000, _json_line("api later")))],
            "/prog-strength/agent": [
                _page(
                    _event(
                        1_000,
                        f"2026-07-30 12:00:00,000 INFO app [request_id={RID}] agent earlier",
                    )
                )
            ],
            "/prog-strength/mcp": [_page()],
        }
    )

    outcome = cloudwatch.search(CFG, RID, WINDOW, limit=500)

    assert [r.service for r in outcome.records] == ["agent", "api"]
    assert outcome.counts == {"api": 1, "agent": 1, "mcp": 0}
    assert outcome.truncated is False


def test_search_queries_every_configured_group(fake_client):
    client = fake_client({})
    cloudwatch.search(CFG, RID, WINDOW, limit=500)
    queried = {call["logGroupName"] for call in client.paginator.calls}
    assert queried == set(CFG.log_groups.values())


def test_search_honours_a_narrowed_service_selection(fake_client):
    cfg = LogsConfig("prod", "us-east-2", None, {"agent": "/prog-strength/agent"})
    client = fake_client({})
    cloudwatch.search(cfg, RID, WINDOW, limit=500)
    assert [c["logGroupName"] for c in client.paginator.calls] == ["/prog-strength/agent"]


def test_search_sends_a_quoted_literal_filter_pattern(stubbed):
    cfg = LogsConfig("prod", "us-east-2", None, {"api": "/prog-strength/api"})
    stubbed.add_response(
        "filter_log_events",
        _page(_event(1_000, _json_line("hit"))),
        expected_params={
            "logGroupName": "/prog-strength/api",
            "startTime": WINDOW.start_ms,
            "endTime": WINDOW.end_ms,
            # Quoted => literal substring match, which works against the api's
            # JSON and the agent/mcp's text lines alike.
            "filterPattern": f'"{RID}"',
        },
    )
    assert len(cloudwatch.search(cfg, RID, WINDOW, limit=500).records) == 1
    stubbed.assert_no_pending_responses()


def test_search_drops_events_not_actually_containing_the_id(stubbed):
    # Belt-and-braces against a filter-pattern surprise widening the result
    # into some other request's lines.
    cfg = LogsConfig("prod", "us-east-2", None, {"api": "/prog-strength/api"})
    stubbed.add_response(
        "filter_log_events",
        _page(
            _event(1_000, _json_line("mine")),
            _event(1_500, '{"level":"INFO","msg":"someone else","request_id":"other"}'),
        ),
    )
    outcome = cloudwatch.search(cfg, RID, WINDOW, limit=500)
    assert len(outcome.records) == 1
    assert "mine" in outcome.records[0].message


def test_search_flags_truncation_rather_than_silently_capping(stubbed):
    cfg = LogsConfig("prod", "us-east-2", None, {"api": "/prog-strength/api"})
    stubbed.add_response(
        "filter_log_events",
        _page(*[_event(1_000 + i, _json_line(f"line {i}")) for i in range(5)]),
    )
    outcome = cloudwatch.search(cfg, RID, WINDOW, limit=3)
    assert len(outcome.records) == 3
    assert outcome.truncated is True


def test_search_with_no_hits_returns_zero_counts(fake_client):
    fake_client({})
    outcome = cloudwatch.search(CFG, RID, WINDOW, limit=500)
    assert outcome.records == []
    assert outcome.counts == {"api": 0, "agent": 0, "mcp": 0}


# --- error translation ----------------------------------------------------


def test_access_denied_names_the_missing_permission(stubbed):
    cfg = LogsConfig("prod", "us-east-2", None, {"api": "/prog-strength/api"})
    stubbed.add_client_error("filter_log_events", service_error_code="AccessDeniedException")
    with pytest.raises(cloudwatch.CloudWatchError, match="logs:FilterLogEvents"):
        cloudwatch.search(cfg, RID, WINDOW, limit=500)


def test_missing_log_group_points_at_the_region(stubbed):
    cfg = LogsConfig("prod", "us-east-2", None, {"api": "/prog-strength/api"})
    stubbed.add_client_error("filter_log_events", service_error_code="ResourceNotFoundException")
    with pytest.raises(cloudwatch.CloudWatchError, match="us-east-2"):
        cloudwatch.search(cfg, RID, WINDOW, limit=500)


def test_expired_credentials_say_to_refresh(stubbed):
    cfg = LogsConfig("prod", "us-east-2", "prog-strength-admin", {"api": "/prog-strength/api"})
    stubbed.add_client_error("filter_log_events", service_error_code="ExpiredTokenException")
    with pytest.raises(cloudwatch.CloudWatchError, match="expired"):
        cloudwatch.search(cfg, RID, WINDOW, limit=500)


def test_missing_credentials_explain_it_is_not_a_pst_token(monkeypatch):
    from botocore.exceptions import NoCredentialsError

    class Boom:
        def get_paginator(self, _name):
            raise NoCredentialsError()

    monkeypatch.setattr(cloudwatch, "build_client", lambda cfg: Boom())
    cfg = LogsConfig("prod", "us-east-2", None, {"api": "/prog-strength/api"})
    with pytest.raises(cloudwatch.CloudWatchError, match="PST_TOKEN"):
        cloudwatch.search(cfg, RID, WINDOW, limit=500)


def test_unknown_profile_is_reported_before_any_query():
    cfg = LogsConfig("prod", "us-east-2", "does-not-exist-anywhere", {"api": "/x"})
    with pytest.raises(cloudwatch.CloudWatchError, match="not found"):
        cloudwatch.search(cfg, RID, WINDOW, limit=500)
