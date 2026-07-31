"""WhoopAdminClient behavior: envelope unwrapping for the admin whoop routes,
request shaping, and error mapping. httpx is mocked at the transport layer with
respx (same approach as test_client.py)."""

import httpx
import pytest
import respx

from prog_strength_tooling.client import APIError, ClientError, WhoopAdminClient
from prog_strength_tooling.config import Config
from prog_strength_tooling.models import WhoopConnection, WhoopResyncOutcome

BASE = "http://api.test"


def _cfg(token="admin-jwt"):
    return Config(api_url=BASE, token=token)


def _ok(data):
    return httpx.Response(200, json={"service": "api", "version": "1", "message": "", "data": data})


def _connection(**overrides):
    conn = {
        "user_id": "u1",
        "whoop_user_id": 12345,
        "status": "active",
        "scopes": "read:recovery read:cycles",
        "token_expires_at": "2026-08-01T12:00:00Z",
        "token_expired": False,
        "connected_at": "2026-06-01T12:00:00Z",
        "updated_at": "2026-07-01T12:00:00Z",
        "latest_recovery_date": "2026-07-30",
        "recovery_row_count": 42,
    }
    conn.update(overrides)
    return conn


def test_missing_token_raises_before_any_request():
    with pytest.raises(ClientError):
        WhoopAdminClient(_cfg(token=None))


@respx.mock
def test_list_connections_unwraps_envelope():
    route = respx.get(f"{BASE}/admin/whoop/connections").mock(
        return_value=_ok({"connections": [_connection(), _connection(user_id="u2")]})
    )
    with WhoopAdminClient(_cfg()) as client:
        result = client.list_connections()

    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(c, WhoopConnection) for c in result)
    assert result[0].user_id == "u1"
    assert result[0].whoop_user_id == 12345
    assert result[1].user_id == "u2"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer admin-jwt"


@respx.mock
def test_list_connections_empty():
    respx.get(f"{BASE}/admin/whoop/connections").mock(return_value=_ok({"connections": []}))
    with WhoopAdminClient(_cfg()) as client:
        result = client.list_connections()
    assert result == []


@respx.mock
def test_get_connection_unwraps_data():
    route = respx.get(f"{BASE}/admin/whoop/connections/u1").mock(
        return_value=_ok(_connection(latest_recovery_date=None))
    )
    with WhoopAdminClient(_cfg()) as client:
        result = client.get_connection("u1")

    assert isinstance(result, WhoopConnection)
    assert result.user_id == "u1"
    assert result.latest_recovery_date is None
    assert route.calls.last.request.headers["Authorization"] == "Bearer admin-jwt"


@respx.mock
def test_get_connection_404_raises_api_error():
    respx.get(f"{BASE}/admin/whoop/connections/nope").mock(
        return_value=httpx.Response(404, json={"error": "connection not found"})
    )
    with WhoopAdminClient(_cfg()) as client:
        with pytest.raises(APIError) as exc:
            client.get_connection("nope")
    assert exc.value.status_code == 404
    assert "not found" in exc.value.message


@respx.mock
def test_resync_sends_body_and_unwraps_outcome():
    route = respx.post(f"{BASE}/admin/whoop/resync").mock(
        return_value=_ok(
            {
                "upserted": 7,
                "skipped_unscored": 1,
                "skipped_no_cycle": 2,
                "skipped_bad_date": 0,
            }
        )
    )
    with WhoopAdminClient(_cfg()) as client:
        result = client.resync("u1", 30)

    assert isinstance(result, WhoopResyncOutcome)
    assert result.upserted == 7
    assert result.skipped_unscored == 1
    assert result.skipped_no_cycle == 2
    assert result.skipped_bad_date == 0

    body = route.calls.last.request.read().decode().replace(" ", "")
    assert '"user_id":"u1"' in body
    assert '"window_days":30' in body


@respx.mock
def test_transport_failure_becomes_client_error():
    respx.get(f"{BASE}/admin/whoop/connections").mock(side_effect=httpx.ConnectError("refused"))
    with WhoopAdminClient(_cfg()) as client:
        with pytest.raises(ClientError):
            client.list_connections()
