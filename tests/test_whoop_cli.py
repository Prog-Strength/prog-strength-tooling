"""End-to-end `pst whoop doctor` / `pst whoop resync` tests via Typer's runner.

The CloudWatch scans are monkeypatched (so no AWS), the admin HTTP calls are
mocked with respx (same approach as test_whoop_client.py). Exit codes carry
meaning here — 0 healthy / 1 findings / 2 config-or-AWS error — so they're
asserted alongside the output.
"""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from prog_strength_tooling import cloudwatch, whooplogs
from prog_strength_tooling.cli import app
from prog_strength_tooling.whooplogs import DeliveryGroup, DeliveryScan, SyncScan

runner = CliRunner()

BASE = "https://api.progstrength.fitness"


@pytest.fixture
def scans(monkeypatch):
    """Install fixture DeliveryScan/SyncScan for the doctor's log checks."""

    def install(deliveries: DeliveryScan, syncs: SyncScan):
        monkeypatch.setattr(whooplogs, "scan_deliveries", lambda *a, **k: deliveries)
        monkeypatch.setattr(whooplogs, "scan_syncs", lambda *a, **k: syncs)

    return install


# --- fixtures for the two canonical states --------------------------------

#: The real outage: 97 POSTs to the trailing-comma path, all 404, no syncs.
OUTAGE_DELIVERIES = DeliveryScan(
    groups=[DeliveryGroup(uri="/webhooks/whoop,", status=404, count=97)]
)
OUTAGE_SYNCS = SyncScan(window_sync_count=0, upserted_total=0)

#: Healthy: deliveries land on the served path with a 2xx and syncs upsert rows.
HEALTHY_DELIVERIES = DeliveryScan(
    groups=[DeliveryGroup(uri="/webhooks/whoop", status=204, count=40)]
)
HEALTHY_SYNCS = SyncScan(window_sync_count=40, upserted_total=120)


def _ok(data):
    return httpx.Response(200, json={"service": "api", "version": "1", "message": "", "data": data})


def _connection(**overrides):
    conn = {
        "user_id": "u1",
        "whoop_user_id": 12345,
        "status": "connected",
        "scopes": "read:recovery",
        "token_expires_at": "2026-08-01T12:00:00Z",
        "token_expired": False,
        "connected_at": "2026-06-01T12:00:00Z",
        "updated_at": "2026-07-31T12:00:00Z",
        "latest_recovery_date": "2026-07-31",
        "recovery_row_count": 42,
    }
    conn.update(overrides)
    return conn


# --- doctor: regression (the outage) --------------------------------------


def test_doctor_regression_reports_check2_and_exits_1(scans):
    scans(OUTAGE_DELIVERIES, OUTAGE_SYNCS)
    # No token: the doctor still runs the log-derived checks (degraded).
    result = runner.invoke(app, ["whoop", "doctor"])
    assert result.exit_code == 1
    # Check 2 (delivery-path) names the offending trailing-comma path.
    assert "/webhooks/whoop," in result.stdout
    assert "delivery-path" in result.stdout


# --- doctor: healthy ------------------------------------------------------


@respx.mock
def test_doctor_healthy_with_token_exits_0(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    respx.get(f"{BASE}/admin/whoop/connections/u1").mock(return_value=_ok(_connection()))
    result = runner.invoke(app, ["whoop", "doctor", "--user", "u1", "--token", "admin-jwt"])
    assert result.exit_code == 0
    # No finding lines: every glyph should be a check mark, not an ✗.
    assert "✗" not in result.stdout


def test_doctor_healthy_no_token_exits_0(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    result = runner.invoke(app, ["whoop", "doctor"])
    # Logs are healthy and admin checks are skipped, not failed -> exit 0.
    assert result.exit_code == 0
    assert "✗" not in result.stdout


# --- doctor: token absent degrades (checks 6/7 skipped) -------------------


def test_doctor_without_token_skips_admin_checks(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    result = runner.invoke(app, ["whoop", "doctor"])
    assert result.exit_code == 0
    assert "connection-health" in result.stdout
    assert "data-freshness" in result.stdout
    # A skipped check must be visibly skipped, not passed off as ok.
    assert "skipped" in result.stdout.lower()


def test_doctor_without_token_still_flags_log_findings(scans):
    scans(OUTAGE_DELIVERIES, OUTAGE_SYNCS)
    result = runner.invoke(app, ["whoop", "doctor"])
    # A log finding is fatal to health even when admin checks are skipped.
    assert result.exit_code == 1


# --- doctor: --json -------------------------------------------------------


def test_doctor_json_is_machine_readable(scans):
    scans(OUTAGE_DELIVERIES, OUTAGE_SYNCS)
    result = runner.invoke(app, ["whoop", "doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["healthy"] is False
    names = [c["name"] for c in payload["checks"]]
    assert "delivery-path" in names
    # The delivery-path finding is present and names the offending path.
    findings = [f for f in payload["findings"]]
    assert any("/webhooks/whoop," in f["evidence"] for f in findings)


# --- doctor: config / AWS error path --------------------------------------


def test_doctor_cloudwatch_error_exits_2(monkeypatch):
    def boom(*a, **k):
        raise cloudwatch.CloudWatchError("access denied reading /prog-strength/api")

    monkeypatch.setattr(whooplogs, "scan_deliveries", boom)
    result = runner.invoke(app, ["whoop", "doctor"])
    assert result.exit_code == 2
    assert "access denied" in result.output


def test_doctor_local_env_exits_2(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    # local ships no CloudWatch logs -> ConfigError -> exit 2.
    result = runner.invoke(app, ["whoop", "doctor", "--env", "local"])
    assert result.exit_code == 2
    assert "docker compose logs" in result.output


# --- resync ---------------------------------------------------------------


@respx.mock
def test_resync_forwards_window_days_and_prints_outcome():
    route = respx.post(f"{BASE}/admin/whoop/resync").mock(
        return_value=_ok(
            {
                "upserted": 5,
                "skipped_unscored": 1,
                "skipped_no_cycle": 2,
                "skipped_bad_date": 0,
            }
        )
    )
    result = runner.invoke(
        app,
        ["whoop", "resync", "--user", "u1", "--days", "200", "--token", "admin-jwt"],
    )
    assert result.exit_code == 0
    # The CLI forwards --days verbatim; the API clamps server-side.
    body = route.calls.last.request.read().decode().replace(" ", "")
    assert '"window_days":200' in body
    assert "5" in result.stdout  # upserted count is shown


@respx.mock
def test_resync_json_output():
    respx.post(f"{BASE}/admin/whoop/resync").mock(
        return_value=_ok(
            {
                "upserted": 5,
                "skipped_unscored": 0,
                "skipped_no_cycle": 0,
                "skipped_bad_date": 0,
            }
        )
    )
    result = runner.invoke(
        app, ["whoop", "resync", "--user", "u1", "--token", "admin-jwt", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["upserted"] == 5


def test_resync_missing_token_exits_nonzero():
    result = runner.invoke(app, ["whoop", "resync", "--user", "u1"])
    assert result.exit_code != 0
    assert "admin token" in result.output


@respx.mock
def test_resync_api_error_maps_to_message_and_nonzero_exit():
    respx.post(f"{BASE}/admin/whoop/resync").mock(
        return_value=httpx.Response(404, json={"error": "no connection for user"})
    )
    result = runner.invoke(app, ["whoop", "resync", "--user", "nope", "--token", "admin-jwt"])
    assert result.exit_code == 1
    assert "no connection for user" in result.output


# --- help -----------------------------------------------------------------


def test_whoop_help_works():
    result = runner.invoke(app, ["whoop", "--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "resync" in result.stdout


def test_doctor_help_works():
    result = runner.invoke(app, ["whoop", "doctor", "--help"])
    assert result.exit_code == 0
    assert "--user" in result.stdout
    assert "--since" in result.stdout
