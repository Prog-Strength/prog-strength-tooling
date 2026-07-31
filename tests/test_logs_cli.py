"""End-to-end `pst logs trace` tests via Typer's runner, CloudWatch faked.

Exit codes carry meaning here (0 hits / 1 no hits / 2 error), so they're
asserted alongside the output.
"""

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from prog_strength_tooling import cloudwatch
from prog_strength_tooling.cli import app
from prog_strength_tooling.logparse import LogRecord

runner = CliRunner()

RID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _record(service="api", level="ERROR", message="nutrition lookup failed", second=0):
    return LogRecord(
        service=service,
        timestamp=datetime(2026, 7, 30, 12, 0, second, tzinfo=UTC),
        level=level,
        message=message,
        raw=json.dumps({"level": level, "msg": message, "request_id": RID}),
        stream="container-abc",
    )


@pytest.fixture
def found(monkeypatch):
    """Make cloudwatch.search return the given records without touching AWS."""

    def install(records, truncated=False):
        outcome = cloudwatch.SearchOutcome(
            records=records,
            counts={"api": len(records), "agent": 0, "mcp": 0},
            truncated=truncated,
        )
        monkeypatch.setattr(cloudwatch, "search", lambda *a, **k: outcome)
        return outcome

    return install


def test_hits_render_and_exit_0(found):
    found([_record(), _record(service="agent", level="INFO", message="chat start", second=1)])
    result = runner.invoke(app, ["logs", "trace", RID])
    assert result.exit_code == 0
    assert "nutrition lookup failed" in result.stdout
    assert "ERROR" in result.stdout
    assert RID in result.stdout


def test_no_hits_exits_1_with_a_widen_hint(found):
    found([])
    result = runner.invoke(app, ["logs", "trace", RID])
    # Exit 1 is "searched fine, found nothing" — distinct from the exit 2 of a
    # broken query, so a script can tell them apart.
    assert result.exit_code == 1
    assert "--since 7d" in result.stdout
    assert "don't propagate" in result.stdout


def test_raw_prints_original_lines(found):
    found([_record()])
    result = runner.invoke(app, ["logs", "trace", RID, "--raw"])
    assert result.exit_code == 0
    assert '"msg"' in result.stdout  # the untouched JSON line


def test_json_output_is_machine_readable(found):
    found([_record()])
    result = runner.invoke(app, ["logs", "trace", RID, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["request_id"] == RID
    assert payload["records"][0]["service"] == "api"
    assert payload["records"][0]["level"] == "ERROR"


def test_truncation_is_announced(found):
    found([_record()], truncated=True)
    result = runner.invoke(app, ["logs", "trace", RID, "--limit", "1"])
    assert result.exit_code == 0
    assert "truncated" in result.output


def test_log_lines_containing_markup_do_not_break_rendering(found):
    # Log bodies are content, not styling. An unbalanced bracket must render
    # literally rather than being swallowed or raising.
    found([_record(message="parse failed near [/dim] token [bold")])
    result = runner.invoke(app, ["logs", "trace", RID])
    assert result.exit_code == 0
    assert "parse failed near" in result.stdout


# --- failures that must not reach AWS -------------------------------------


def test_local_env_explains_there_are_no_cloudwatch_logs():
    result = runner.invoke(app, ["logs", "trace", RID, "--env", "local"])
    assert result.exit_code == 2
    assert "docker compose logs" in result.output


def test_unknown_service_is_rejected():
    result = runner.invoke(app, ["logs", "trace", RID, "--service", "web"])
    assert result.exit_code == 2
    assert "unknown service" in result.output


def test_bad_duration_is_rejected():
    result = runner.invoke(app, ["logs", "trace", RID, "--since", "24"])
    assert result.exit_code == 2
    assert "invalid duration" in result.output


def test_since_with_explicit_bounds_is_rejected():
    result = runner.invoke(
        app, ["logs", "trace", RID, "--since", "7d", "--start", "2026-07-29T10:00:00"]
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_empty_request_id_is_rejected():
    result = runner.invoke(app, ["logs", "trace", "   "])
    assert result.exit_code == 2
    assert "empty" in result.output


def test_cloudwatch_error_exits_2(monkeypatch):
    def boom(*a, **k):
        raise cloudwatch.CloudWatchError("access denied reading /prog-strength/api")

    monkeypatch.setattr(cloudwatch, "search", boom)
    result = runner.invoke(app, ["logs", "trace", RID])
    assert result.exit_code == 2
    assert "access denied" in result.output


def test_non_hex_request_id_is_accepted(found):
    # The api adopts any inbound X-Request-ID, so a client-supplied id like
    # this is legitimate — rejecting it would break the main use case.
    found([_record()])
    result = runner.invoke(app, ["logs", "trace", "trace-from-edge-abc123"])
    assert result.exit_code == 0
