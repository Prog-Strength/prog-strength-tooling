"""End-to-end `pst whoop doctor` / `pst whoop resync` tests via Typer's runner.

The CloudWatch scans are monkeypatched (so no AWS), the admin HTTP calls are
mocked with respx (same approach as test_whoop_client.py). Exit codes carry
meaning here — 0 healthy / 1 findings / 2 config-or-AWS error — so they're
asserted alongside the output.
"""

import json
import re

import httpx
import pytest
import respx
from typer.testing import CliRunner

from prog_strength_tooling import cloudwatch, whooplogs
from prog_strength_tooling.cli import app
from prog_strength_tooling.whooplogs import DeliveryGroup, DeliveryScan, SyncScan, WhoopScan

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI colour so help-text assertions don't depend on whether the
    runner (locally) or CI (FORCE_COLOR) renders rich output with escapes."""
    return _ANSI.sub("", text)


def _unwrap(text: str) -> str:
    """Strip colour and collapse whitespace, so prose assertions survive wrapping.

    rich reflows the truncation banner to the console width, which can drop a
    newline into the middle of any phrase. Asserting on the unwrapped form
    tests what the sentence SAYS rather than where it happened to break.
    """
    return " ".join(_plain(text).split())


BASE = "https://api.progstrength.fitness"


@pytest.fixture
def scans(monkeypatch):
    """Install a fixture WhoopScan for the doctor's log checks."""

    def install(deliveries: DeliveryScan, syncs: SyncScan, *, truncated: bool = False):
        result = _scan(deliveries, syncs, truncated=truncated)
        monkeypatch.setattr(whooplogs, "scan", lambda *a, **k: result)
        return result

    return install


def _scan(deliveries, syncs, *, truncated=False, events=1204, pages=3):
    """A WhoopScan wrapping fixture aggregations, as the command would build it."""
    from datetime import UTC, datetime

    return WhoopScan(
        deliveries=deliveries,
        syncs=syncs,
        events_scanned=events,
        pages=pages,
        truncated=truncated,
        covered_start=datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
        covered_end=datetime(2026, 7, 30, 9, 15, tzinfo=UTC),
    )


def _diagnose(scan):
    """Run the engine over a fixture scan with no admin evidence (checks 6/7 skip).

    `diagnose` takes `now` positionally with no default, so it is passed here.
    """
    from datetime import UTC, datetime

    from prog_strength_tooling.whoop import diagnose

    return diagnose(scan.deliveries, scan.syncs, None, False, datetime.now(UTC))


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

    monkeypatch.setattr(whooplogs, "scan", boom)
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
    out = _plain(result.stdout)
    assert "doctor" in out
    assert "resync" in out


def test_doctor_help_works():
    result = runner.invoke(app, ["whoop", "doctor", "--help"])
    assert result.exit_code == 0
    out = _plain(result.stdout)
    assert "--user" in out
    assert "--since" in out


# --- truncation: a partial read must never read as a complete diagnosis ---


def test_diagnosis_shows_a_truncation_banner(capsys):
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    render_diagnosis(_diagnose(scan), scan, as_json=False)

    out = _unwrap(capsys.readouterr().out)
    assert "results truncated" in out
    assert "2026-07-29 08:00Z" in out


def test_truncation_banner_says_which_end_of_the_window_was_read(capsys):
    """The load-bearing fact: a capped scan keeps the OLDEST slice.

    CloudWatch returns events ascending by timestamp, so truncation drops the
    most recent events — exactly the ones the freshness and delivery checks
    care about. A banner reporting only a bare range lets an operator assume
    the recent hours were covered, which is the misreading the banner exists
    to prevent.
    """
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    render_diagnosis(_diagnose(scan), scan, as_json=False)

    out = _unwrap(capsys.readouterr().out)
    assert "OLDEST slice of the requested window" in out
    assert "oldest-first" in out
    # It must say the recent events are MISSING, not merely that data is old.
    assert "more recent events were not read" in out


def test_truncation_banner_recommends_remedies_that_actually_work(capsys):
    """Raising the cap reads further from the same oldest start — not a fix.

    Only narrowing --since (moving the window's start) or --max-events 0
    (reading all of it) restores recent coverage, so the banner must not
    offer a cap bump as an equally good option.
    """
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    render_diagnosis(_diagnose(scan), scan, as_json=False)

    out = _unwrap(capsys.readouterr().out)
    assert "Narrow --since, or pass --max-events 0" in out
    assert "raise --max-events" not in out


def test_diagnosis_has_no_banner_when_complete(capsys):
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    render_diagnosis(_diagnose(scan), scan, as_json=False)

    assert "truncated" not in _plain(capsys.readouterr().out)


def test_diagnosis_json_carries_the_scan_metadata(capsys):
    from prog_strength_tooling.render import render_diagnosis

    scan = _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    render_diagnosis(_diagnose(scan), scan, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["scan"]["truncated"] is True
    assert payload["scan"]["events_scanned"] == 1204
    assert payload["scan"]["pages"] == 3
    assert payload["scan"]["covered_start"].startswith("2026-07-29T08:00")


def test_doctor_forwards_max_events_to_the_scan(monkeypatch):
    seen = {}

    def capture(cfg, window, max_events):
        seen["max_events"] = max_events
        return _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS)

    monkeypatch.setattr(whooplogs, "scan", capture)
    result = runner.invoke(app, ["whoop", "doctor", "--max-events", "500"])
    assert result.exit_code == 0
    assert seen["max_events"] == 500


def test_doctor_defaults_to_the_module_cap(monkeypatch):
    seen = {}

    def capture(cfg, window, max_events):
        seen["max_events"] = max_events
        return _scan(HEALTHY_DELIVERIES, HEALTHY_SYNCS)

    monkeypatch.setattr(whooplogs, "scan", capture)
    runner.invoke(app, ["whoop", "doctor"])
    assert seen["max_events"] == whooplogs.MAX_EVENTS


def test_doctor_rejects_a_negative_max_events(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS)
    result = runner.invoke(app, ["whoop", "doctor", "--max-events", "-1"])
    assert result.exit_code == 2
    assert "--max-events" in result.output


def test_doctor_banners_a_truncated_scan(scans):
    scans(HEALTHY_DELIVERIES, HEALTHY_SYNCS, truncated=True)
    result = runner.invoke(app, ["whoop", "doctor"])
    # Still healthy — truncation is a caveat on the evidence, not a finding.
    assert result.exit_code == 0
    assert "results truncated" in _unwrap(result.output)


def test_doctor_json_reports_truncation(scans):
    scans(OUTAGE_DELIVERIES, OUTAGE_SYNCS, truncated=True)
    result = runner.invoke(app, ["whoop", "doctor", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["scan"]["truncated"] is True
