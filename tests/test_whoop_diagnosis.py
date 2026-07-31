"""whoop.diagnose — the pure seven-check doctor engine.

No network here: the command layer (Task T5) does the CloudWatch/HTTP calls;
diagnose() takes the already-gathered DeliveryScan/SyncScan/WhoopConnection and
returns a Diagnosis. Tests construct those evidence objects directly and assert
on which checks fire, what their findings say, and which degrade to skipped.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prog_strength_tooling import whoop
from prog_strength_tooling.models import WhoopConnection
from prog_strength_tooling.whooplogs import DeliveryGroup, DeliveryScan, SyncScan

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)

EXPECTED_URL = "https://api.progstrength.fitness/webhooks/whoop"


def _connection(**overrides) -> WhoopConnection:
    """A healthy-by-default WhoopConnection; override one field per test."""
    fields = dict(
        user_id="u1",
        whoop_user_id=123,
        status="connected",
        scopes="read:recovery",
        token_expires_at="2026-08-01T00:00:00Z",
        token_expired=False,
        connected_at="2026-07-01T00:00:00Z",
        updated_at="2026-07-31T00:00:00Z",
        latest_recovery_date="2026-07-31",
        recovery_row_count=42,
    )
    fields.update(overrides)
    return WhoopConnection(**fields)


def _by_name(diagnosis: whoop.Diagnosis, name: str) -> whoop.CheckResult:
    for check in diagnosis.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c.name for c in diagnosis.checks]}")


# --- Outage fixture: WHOOP posting to the wrong path, no admin token ----------


def test_outage_check2_names_offending_path_and_expected_url():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop,", 404, 97)]),
        syncs=SyncScan(0, 0),
        connection=None,
        token_present=False,
        now=NOW,
    )

    assert not diagnosis.healthy

    path_check = _by_name(diagnosis, "delivery-path")
    assert path_check.ok is False
    assert path_check.finding is not None
    # The headline finding must name the concrete offending path...
    assert "/webhooks/whoop," in path_check.finding.evidence
    # ...and point at the exact URL WHOOP should be posting to.
    assert EXPECTED_URL in path_check.finding.fix or EXPECTED_URL in path_check.finding.evidence


def test_outage_checks_6_and_7_skipped_without_token():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop,", 404, 97)]),
        syncs=SyncScan(0, 0),
        connection=None,
        token_present=False,
        now=NOW,
    )

    conn = _by_name(diagnosis, "connection-health")
    fresh = _by_name(diagnosis, "data-freshness")
    assert conn.skipped is True
    assert fresh.skipped is True
    assert conn.reason and "PST_TOKEN" in conn.reason
    assert conn.finding is None and fresh.finding is None


# --- Healthy fixture: everything green ---------------------------------------


def test_healthy_has_no_findings_and_no_skips():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(latest_recovery_date="2026-07-31"),
        token_present=True,
        now=NOW,
    )

    assert diagnosis.healthy is True
    assert diagnosis.findings == []
    assert all(not c.skipped for c in diagnosis.checks)
    assert len(diagnosis.checks) == 7


# --- Degraded (no token) but otherwise healthy logs --------------------------


def test_degraded_skips_6_7_but_runs_log_checks():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=None,
        token_present=False,
        now=NOW,
    )

    # Log checks (1-5) all ran and passed.
    for name in (
        "deliveries-arriving",
        "delivery-path",
        "signatures-accepted",
        "deliveries-producing-syncs",
        "syncs-landing-rows",
    ):
        check = _by_name(diagnosis, name)
        assert check.skipped is False
        assert check.finding is None

    # Admin checks skipped -> healthy reflects only log checks.
    assert _by_name(diagnosis, "connection-health").skipped is True
    assert _by_name(diagnosis, "data-freshness").skipped is True
    assert diagnosis.healthy is True


def test_degraded_with_token_present_but_connection_none_still_skips():
    # A token may be present yet no connection row came back (empty admin list);
    # 6/7 have nothing to judge, so they still skip rather than false-fire.
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=None,
        token_present=True,
        now=NOW,
    )
    assert _by_name(diagnosis, "connection-health").skipped is True
    assert _by_name(diagnosis, "data-freshness").skipped is True


# --- Check 1: deliveries-arriving --------------------------------------------


def test_check1_fires_on_empty_delivery_scan():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([]),
        syncs=SyncScan(0, 0),
        connection=None,
        token_present=False,
        now=NOW,
    )
    arriving = _by_name(diagnosis, "deliveries-arriving")
    assert arriving.ok is False
    assert arriving.finding is not None
    assert not diagnosis.healthy


# --- Check 2: delivery-path fires on non-2xx even at the right path -----------


def test_check2_fires_on_non_2xx_at_correct_path():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 500, 3)]),
        syncs=SyncScan(0, 0),
        connection=None,
        token_present=False,
        now=NOW,
    )
    path_check = _by_name(diagnosis, "delivery-path")
    assert path_check.ok is False
    assert path_check.finding is not None


# --- Check 3: signatures-accepted fires on 401 -------------------------------


def test_check3_fires_on_401():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 401, 8)]),
        syncs=SyncScan(0, 0),
        connection=None,
        token_present=False,
        now=NOW,
    )
    sig = _by_name(diagnosis, "signatures-accepted")
    assert sig.ok is False
    assert sig.finding is not None
    assert "401" in sig.finding.evidence


# --- Check 4: deliveries-producing-syncs -------------------------------------


def test_check4_fires_when_deliveries_but_no_syncs():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 10)]),
        syncs=SyncScan(window_sync_count=0, upserted_total=0),
        connection=None,
        token_present=False,
        now=NOW,
    )
    check = _by_name(diagnosis, "deliveries-producing-syncs")
    assert check.ok is False
    assert check.finding is not None


# --- Check 5: syncs-landing-rows ---------------------------------------------


def test_check5_fires_when_syncs_ran_but_no_rows():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 10)]),
        syncs=SyncScan(window_sync_count=3, upserted_total=0),
        connection=None,
        token_present=False,
        now=NOW,
    )
    check = _by_name(diagnosis, "syncs-landing-rows")
    assert check.ok is False
    assert check.finding is not None


# --- Check 6: connection-health ----------------------------------------------


def test_check6_fires_on_disconnected_status():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(status="revoked"),
        token_present=True,
        now=NOW,
    )
    check = _by_name(diagnosis, "connection-health")
    assert check.skipped is False
    assert check.ok is False
    assert check.finding is not None
    assert "revoked" in check.finding.evidence


def test_check6_fires_on_expired_token():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(token_expired=True),
        token_present=True,
        now=NOW,
    )
    check = _by_name(diagnosis, "connection-health")
    assert check.ok is False
    assert check.finding is not None


# --- Check 7: data-freshness -------------------------------------------------


def test_check7_fires_when_recovery_stale():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(latest_recovery_date="2026-07-01"),
        token_present=True,
        now=NOW,
    )
    check = _by_name(diagnosis, "data-freshness")
    assert check.ok is False
    assert check.finding is not None
    assert "2026-07-01" in check.finding.evidence


def test_check7_fires_when_no_recovery_date():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(latest_recovery_date=None),
        token_present=True,
        now=NOW,
    )
    check = _by_name(diagnosis, "data-freshness")
    assert check.ok is False
    assert check.finding is not None


def test_check7_passes_within_48h():
    recent = (NOW - timedelta(hours=12)).date().isoformat()
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(latest_recovery_date=recent),
        token_present=True,
        now=NOW,
    )
    check = _by_name(diagnosis, "data-freshness")
    assert check.ok is True
    assert check.finding is None


# --- Diagnosis order ---------------------------------------------------------


def test_checks_are_in_sow_order():
    diagnosis = whoop.diagnose(
        deliveries=DeliveryScan([DeliveryGroup("/webhooks/whoop", 204, 5)]),
        syncs=SyncScan(window_sync_count=2, upserted_total=5),
        connection=_connection(),
        token_present=True,
        now=NOW,
    )
    assert [c.name for c in diagnosis.checks] == [
        "deliveries-arriving",
        "delivery-path",
        "signatures-accepted",
        "deliveries-producing-syncs",
        "syncs-landing-rows",
        "connection-health",
        "data-freshness",
    ]
