"""End-to-end CLI tests via Typer's runner, with the HTTP layer mocked."""

import httpx
import respx
from typer.testing import CliRunner

from prog_strength_tooling.cli import app

runner = CliRunner()
BASE = "http://api.test"
ENV = {"PST_API_URL": BASE, "PST_TOKEN": "admin-jwt"}


def _ok(data):
    return httpx.Response(200, json={"service": "api", "version": "1", "message": "", "data": data})


@respx.mock
def test_memory_list_renders_table():
    respx.get(f"{BASE}/admin/memories").mock(
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
    result = runner.invoke(app, ["memory", "list", "--user", "u1"], env=ENV)
    assert result.exit_code == 0
    # rich folds the cell to the non-tty 80-col width, so assert on a token
    # that fits on one line rather than the full phrase.
    assert "dumbbells" in result.stdout


@respx.mock
def test_memory_search_json_output():
    respx.post(f"{BASE}/admin/memories/search").mock(
        return_value=_ok(
            {
                "threshold": 0.7,
                "matches": [
                    {
                        "text": "benches 135",
                        "distance": 0.42,
                        "source_session_id": "s2",
                        "created_at": "2026-06-02T08:00:00Z",
                    }
                ],
            }
        )
    )
    result = runner.invoke(
        app, ["memory", "search", "--user", "u1", "--query", "bench", "--json"], env=ENV
    )
    assert result.exit_code == 0
    assert "benches 135" in result.stdout
    assert "0.7" in result.stdout


@respx.mock
def test_missing_token_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("PST_TOKEN", raising=False)
    route = respx.get(f"{BASE}/admin/memories").mock(return_value=_ok({"memories": []}))

    result = runner.invoke(app, ["memory", "list", "--user", "u1"], env={"PST_API_URL": BASE})

    assert result.exit_code == 1
    assert not route.called  # gated up front, not by a 401 from the server
    out = result.output
    assert "missing admin token" in out
    assert "PST_TOKEN" in out and "--token" in out  # how to supply one
    assert "auth/dev/token" in out  # how to obtain one


@respx.mock
def test_missing_token_gates_search_too(monkeypatch):
    monkeypatch.delenv("PST_TOKEN", raising=False)
    route = respx.post(f"{BASE}/admin/memories/search").mock(
        return_value=_ok({"threshold": 0.7, "matches": []})
    )

    result = runner.invoke(
        app, ["memory", "search", "--user", "u1", "--query", "bench"], env={"PST_API_URL": BASE}
    )

    assert result.exit_code == 1
    assert not route.called
    assert "missing admin token" in result.output


def test_status_needs_no_token(monkeypatch):
    # Health endpoints are public — the token gate must not spread to status.
    monkeypatch.delenv("PST_TOKEN", raising=False)
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    assert "missing admin token" not in result.output


def test_no_args_shows_help():
    # no_args_is_help prints help and exits 2 (Click's no-command convention).
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "memory" in result.output
