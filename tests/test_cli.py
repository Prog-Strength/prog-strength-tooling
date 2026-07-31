"""End-to-end CLI tests via Typer's runner, with the HTTP layer mocked."""

import re

import httpx
import respx
from typer.testing import CliRunner

from prog_strength_tooling.cli import app

runner = CliRunner()
BASE = "http://api.test"
ENV = {"PST_API_URL": BASE, "PST_TOKEN": "admin-jwt"}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI colour so help-text assertions don't depend on whether the
    runner (locally) or CI (FORCE_COLOR) renders rich output with escapes."""
    return _ANSI.sub("", text)


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


def test_root_help_still_shows_the_app_description():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = _plain(result.output)
    assert "CLI tooling for Prog Strength Backend" in out
    assert "--verbose" in out
    assert "--quiet" in out


def test_verbose_flag_is_accepted_before_a_subcommand():
    result = runner.invoke(app, ["-v", "status", "--help"])
    assert result.exit_code == 0


def test_verbose_flag_sets_the_debug_level():
    import logging

    from prog_strength_tooling import logsetup

    runner.invoke(app, ["-v", "status", "--help"])
    assert logging.getLogger(logsetup.ROOT_LOGGER).level == logging.DEBUG


def test_quiet_flag_sets_the_warning_level():
    import logging

    from prog_strength_tooling import logsetup

    runner.invoke(app, ["-q", "status", "--help"])
    assert logging.getLogger(logsetup.ROOT_LOGGER).level == logging.WARNING


def test_startup_debug_line_redacts_a_token(monkeypatch, caplog):
    # CliRunner.invoke never touches sys.argv (Click calls cli.main(args=...)
    # directly), so the callback's argv line reads whatever process actually
    # launched — here that's pytest's own argv. Set it explicitly so this test
    # proves redaction on the line the callback really emits, rather than
    # passing vacuously regardless of whether redact_argv works at all.
    import logging
    import sys

    monkeypatch.setattr(sys, "argv", ["pst", "--token", "SUPERSECRET", "status", "--help"])
    with caplog.at_level(logging.DEBUG, logger="prog_strength_tooling"):
        runner.invoke(app, ["-v", "status", "--help"])

    argv_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("argv:")]
    assert argv_lines, "expected an argv DEBUG line"
    assert "SUPERSECRET" not in " ".join(argv_lines)
    assert "***" in argv_lines[0]
