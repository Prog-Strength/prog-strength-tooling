"""Client behavior: envelope unwrapping, the omit-vs-explicit-zero body
contract, and error mapping. httpx is mocked at the transport layer with
respx (same approach as the agent repo)."""

import httpx
import pytest
import respx

from prog_strength_tooling.client import APIError, ClientError, MemoryClient
from prog_strength_tooling.config import Config

BASE = "http://api.test"


def _cfg(token="admin-jwt"):
    return Config(api_url=BASE, token=token)


def _ok(data):
    return httpx.Response(200, json={"service": "api", "version": "1", "message": "", "data": data})


def test_missing_token_raises_before_any_request():
    with pytest.raises(ClientError):
        MemoryClient(_cfg(token=None))


@respx.mock
def test_list_unwraps_envelope_and_sends_user_filter():
    route = respx.get(f"{BASE}/admin/memories").mock(
        return_value=_ok(
            {
                "memories": [
                    {
                        "distilled_text": "prefers dumbbells",
                        "user_id": "u1",
                        "source_session_id": "s1",
                        "embedding_model": "voyage-3",
                        "embedding_dim": 1024,
                        "created_at": "2026-06-01T12:00:00Z",
                    }
                ]
            }
        )
    )
    with MemoryClient(_cfg()) as client:
        result = client.list_memories("u1", limit=50, offset=10)

    assert len(result.memories) == 1
    assert result.memories[0].distilled_text == "prefers dumbbells"
    assert result.memories[0].superseded_at is None
    sent = route.calls.last.request
    assert sent.url.params["user_id"] == "u1"
    assert sent.url.params["limit"] == "50"
    assert sent.url.params["offset"] == "10"
    assert sent.headers["Authorization"] == "Bearer admin-jwt"


@respx.mock
def test_search_omits_unset_k_and_threshold():
    route = respx.post(f"{BASE}/admin/memories/search").mock(
        return_value=_ok({"threshold": 0.7, "matches": []})
    )
    with MemoryClient(_cfg()) as client:
        client.search("u1", "how much do I bench")

    body = route.calls.last.request.read().decode()
    assert '"query"' in body and '"user_id"' in body
    assert "k" not in body
    assert "threshold" not in body


@respx.mock
def test_search_sends_explicit_zero_threshold():
    # 0 means full sweep (no cap) and MUST be forwarded, unlike an unset cap.
    route = respx.post(f"{BASE}/admin/memories/search").mock(
        return_value=_ok({"threshold": 0.0, "matches": []})
    )
    with MemoryClient(_cfg()) as client:
        result = client.search("u1", "q", k=5, threshold=0.0)

    body = route.calls.last.request.read().decode()
    assert '"threshold":0' in body.replace(" ", "")
    assert '"k":5' in body.replace(" ", "")
    assert result.threshold == 0.0


@respx.mock
def test_non_2xx_becomes_api_error_with_server_message():
    respx.get(f"{BASE}/admin/memories").mock(
        return_value=httpx.Response(403, json={"error": "forbidden: not an admin"})
    )
    with MemoryClient(_cfg()) as client:
        with pytest.raises(APIError) as exc:
            client.list_memories("u1")
    assert exc.value.status_code == 403
    assert "forbidden" in exc.value.message


@respx.mock
def test_transport_failure_becomes_client_error():
    respx.get(f"{BASE}/admin/memories").mock(side_effect=httpx.ConnectError("refused"))
    with MemoryClient(_cfg()) as client:
        with pytest.raises(ClientError):
            client.list_memories("u1")


@respx.mock
def test_client_logs_method_path_status_and_duration(caplog):
    import logging

    respx.get("https://api.progstrength.fitness/admin/memories").mock(
        return_value=httpx.Response(200, json={"data": {"memories": [], "count": 0}})
    )
    cfg = Config(api_url="https://api.progstrength.fitness", token="admin-token-value-1234")

    with caplog.at_level(logging.INFO, logger="prog_strength_tooling"):
        with MemoryClient(cfg) as client:
            client.list_memories(user_id="u1")

    line = " ".join(r.getMessage() for r in caplog.records)
    assert "GET" in line
    assert "/admin/memories" in line
    assert "status=200" in line
    assert "elapsed_ms=" in line


@respx.mock
def test_client_never_logs_the_bearer_token(caplog):
    import logging

    secret = "eyJhbGciOiJIUzI1NiJ9.admin-token-payload.sig"
    respx.get("https://api.progstrength.fitness/admin/memories").mock(
        return_value=httpx.Response(200, json={"data": {"memories": [], "count": 0}})
    )
    cfg = Config(api_url="https://api.progstrength.fitness", token=secret)

    with caplog.at_level(logging.DEBUG, logger="prog_strength_tooling"):
        with MemoryClient(cfg) as client:
            client.list_memories(user_id="u1")

    for record in caplog.records:
        assert secret not in record.getMessage()
