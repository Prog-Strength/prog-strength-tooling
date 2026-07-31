"""The WHOOP integration doctor's diagnosis engine.

Runs the seven SOW checks over already-gathered evidence and returns a
`Diagnosis`. This module is deliberately PURE: it imports no boto3/httpx and
makes no calls. The command layer (Task T5) does the CloudWatch/HTTP work and
hands the results in — a `DeliveryScan`, a `SyncScan`, and (when an admin token
is available) a `WhoopConnection`. Keeping the engine pure makes every check
trivially testable from constructed fixtures and keeps the "chain" logic in one
readable place.

The checks form a delivery-to-dashboard chain: is WHOOP even POSTing (1), to
the path we serve (2), with a signature we accept (3); do those deliveries turn
into syncs (4); do those syncs land rows (5); and — when we can see the admin
side — is the connection healthy (6) and the data fresh (7).

Checks 6 and 7 are admin-API-derived. When no admin token is present (or the
admin list returned no connection row), they degrade to *skipped* with a reason
rather than failing: a missing token must never block the five log-derived
checks, which need only AWS creds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import WhoopConnection
from .whooplogs import DeliveryScan, SyncScan

#: The one path the api actually serves the WHOOP webhook on
#: (internal/server: r.Post("/webhooks/whoop", …)). Every corrective action for
#: a misrouted delivery points back to this exact URL.
EXPECTED_WEBHOOK_URL = "https://api.progstrength.fitness/webhooks/whoop"
EXPECTED_WEBHOOK_PATH = "/webhooks/whoop"

#: A recovery row older than this (relative to `now`) means data has stopped
#: landing even if the plumbing looks fine — WHOOP scores recovery once per
#: day, so 48h allows for one fully missed day before we call it stale.
FRESHNESS_MAX_AGE = timedelta(hours=48)

#: Shown on both admin checks when they can't run.
SKIP_REASON = "no admin token; run with PST_TOKEN to include connection + freshness checks"


@dataclass(frozen=True)
class Finding:
    """A single failed check, as structured fields (rendering is Task T5)."""

    check: str
    symptom: str
    evidence: str
    fix: str


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one of the seven checks.

    `ok` is meaningful only when `skipped` is False. A skipped check carries a
    `reason` and no finding; a failed check carries a `finding`.
    """

    name: str
    ok: bool
    skipped: bool = False
    reason: str | None = None
    finding: Finding | None = None


@dataclass(frozen=True)
class Diagnosis:
    """The seven check results, in SOW order."""

    checks: list[CheckResult]

    @property
    def findings(self) -> list[Finding]:
        return [c.finding for c in self.checks if c.finding is not None]

    @property
    def healthy(self) -> bool:
        # Healthy = no findings among non-skipped checks. Skipped checks (6/7
        # without a token) don't count against health — they simply weren't run.
        return not self.findings


def _is_2xx(status: int) -> bool:
    return 200 <= status < 300


def _total_deliveries(deliveries: DeliveryScan) -> int:
    return sum(g.count for g in deliveries.groups)


def _check_deliveries_arriving(deliveries: DeliveryScan) -> CheckResult:
    """Check 1: any whoop POST at all reached the api in the window."""
    name = "deliveries-arriving"
    if deliveries.groups:
        return CheckResult(name=name, ok=True)
    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="No WHOOP deliveries reached the api in the window.",
            evidence="No POST to a whoop path was logged.",
            fix=(
                "Confirm WHOOP is configured to send webhooks and that the api is "
                f"reachable. Expected: {EXPECTED_WEBHOOK_URL}"
            ),
        ),
    )


def _check_delivery_path(deliveries: DeliveryScan) -> CheckResult:
    """Check 2 (the headline): deliveries hit the served path with a 2xx.

    Fires when any delivery group's uri isn't exactly ``/webhooks/whoop`` OR its
    status isn't 2xx. The classic failure is a stray trailing comma in the
    WHOOP dashboard URL ("/webhooks/whoop,") that misses the route and 404s. The
    finding names the concrete offending path(s) and the exact URL WHOOP should
    post to, so the fix is copy-pasteable.
    """
    name = "delivery-path"
    if not deliveries.groups:
        # Nothing to judge — check 1 owns the "no deliveries" finding, and we
        # don't want two findings for one root cause.
        return CheckResult(name=name, ok=True)

    offenders = [
        g for g in deliveries.groups if g.uri != EXPECTED_WEBHOOK_PATH or not _is_2xx(g.status)
    ]
    if not offenders:
        return CheckResult(name=name, ok=True)

    detail = ", ".join(f"{g.count} → {g.uri} ({g.status})" for g in offenders)
    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="WHOOP is posting to a path chi does not serve (or is being rejected).",
            evidence=detail,
            fix=f"WHOOP Developer Dashboard → webhook URL. Expected: {EXPECTED_WEBHOOK_URL}",
        ),
    )


def _check_signatures_accepted(deliveries: DeliveryScan) -> CheckResult:
    """Check 3: no delivery was rejected as unauthenticated (401).

    A 401 specifically means the api received the webhook but rejected its
    signature — a client-secret / signing-key mismatch, not a routing problem.
    """
    name = "signatures-accepted"
    unauthorized = [g for g in deliveries.groups if g.status == 401]
    if not unauthorized:
        return CheckResult(name=name, ok=True)

    detail = ", ".join(f"{g.count} → {g.uri} (401)" for g in unauthorized)
    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="WHOOP webhook deliveries are being rejected as unauthenticated.",
            evidence=detail,
            fix=(
                "Signature verification is failing. Confirm the WHOOP client "
                "secret / signing key configured on the api matches the WHOOP app."
            ),
        ),
    )


def _check_deliveries_producing_syncs(deliveries: DeliveryScan, syncs: SyncScan) -> CheckResult:
    """Check 4: accepted deliveries actually triggered window syncs."""
    name = "deliveries-producing-syncs"
    total = _total_deliveries(deliveries)
    if total == 0 or syncs.window_sync_count > 0:
        # No deliveries → nothing should have synced (check 1 owns that gap).
        # Any window syncs → the delivery→sync step is working.
        return CheckResult(name=name, ok=True)

    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="Deliveries arrived but no window sync ran.",
            evidence=f"{total} deliveries in window, 0 window syncs logged.",
            fix=(
                "Deliveries are reaching the api but not producing syncs. Check the "
                "whoopsync worker / handler for errors between webhook receipt and sync."
            ),
        ),
    )


def _check_syncs_landing_rows(syncs: SyncScan) -> CheckResult:
    """Check 5: window syncs that ran actually upserted rows.

    Interpretation note: the SOW frames this as "syncs landing rows" and hints
    at per-disposition skip counts, but `SyncScan` today exposes only
    `window_sync_count` and `upserted_total` — no per-reason skip fields. So we
    implement the observable signal that IS available: a sync ran
    (window_sync_count > 0) yet upserted zero rows (upserted_total == 0), which
    is the shape of a silently-empty dashboard. We deliberately do NOT invent
    skip-count fields that don't exist on SyncScan.
    """
    name = "syncs-landing-rows"
    if syncs.window_sync_count == 0 or syncs.upserted_total > 0:
        return CheckResult(name=name, ok=True)

    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="Window syncs ran but landed no rows.",
            evidence=f"{syncs.window_sync_count} window syncs upserted 0 rows total.",
            fix=(
                "Syncs are completing but every record is being skipped (unscored / "
                "no cycle / bad date) or the write is failing. Inspect the sync-complete "
                "log lines' per-reason skip counts."
            ),
        ),
    )


def _check_connection_health(
    connection: WhoopConnection | None, token_present: bool
) -> CheckResult:
    """Check 6 (admin): the WHOOP connection is connected with a live token."""
    name = "connection-health"
    if not token_present or connection is None:
        return CheckResult(name=name, ok=False, skipped=True, reason=SKIP_REASON)

    problems = []
    if connection.status != "connected":
        problems.append(f"status={connection.status}")
    if connection.token_expired:
        problems.append("token expired")

    if not problems:
        return CheckResult(name=name, ok=True)

    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="The WHOOP connection is not in a healthy connected state.",
            evidence=", ".join(problems),
            fix=(
                "The user must reconnect WHOOP (re-authorize) so the api holds a valid, "
                "unexpired token with the connection marked connected."
            ),
        ),
    )


def _check_data_freshness(
    connection: WhoopConnection | None, token_present: bool, now: datetime
) -> CheckResult:
    """Check 7 (admin): the newest recovery row is within 48h of now.

    A None `latest_recovery_date` means no recovery data has ever landed, which
    is the worst case — treated as stale (a finding), not skipped.
    """
    name = "data-freshness"
    if not token_present or connection is None:
        return CheckResult(name=name, ok=False, skipped=True, reason=SKIP_REASON)

    latest = connection.latest_recovery_date
    if latest is None:
        return CheckResult(
            name=name,
            ok=False,
            finding=Finding(
                check=name,
                symptom="No recovery data has landed for this connection.",
                evidence="latest_recovery_date is null.",
                fix="No recovery rows ingested. Trigger a resync once deliveries/syncs are healthy.",
            ),
        )

    try:
        latest_dt = datetime.strptime(latest, "%Y-%m-%d")
    except ValueError:
        # An unparseable date is itself a red flag; surface it rather than crash.
        return CheckResult(
            name=name,
            ok=False,
            finding=Finding(
                check=name,
                symptom="The latest recovery date is unparseable.",
                evidence=f"latest_recovery_date={latest!r} (expected YYYY-MM-DD).",
                fix="Investigate how the api recorded this recovery date.",
            ),
        )

    # `now` may be tz-aware while the parsed date is naive; compare as naive UTC
    # dates since latest_recovery_date is a calendar date with no zone.
    now_naive = now.replace(tzinfo=None)
    if now_naive - latest_dt <= FRESHNESS_MAX_AGE:
        return CheckResult(name=name, ok=True)

    return CheckResult(
        name=name,
        ok=False,
        finding=Finding(
            check=name,
            symptom="The newest recovery is older than 48h — fresh data has stopped landing.",
            evidence=f"latest_recovery_date={latest} (now {now_naive.date().isoformat()}).",
            fix=(
                "Data has gone stale. Once the delivery/sync chain is healthy, trigger a "
                "resync to backfill the missing window."
            ),
        ),
    )


def diagnose(
    deliveries: DeliveryScan,
    syncs: SyncScan,
    connection: WhoopConnection | None,
    token_present: bool,
    now: datetime,
) -> Diagnosis:
    """Run the seven checks over gathered evidence and return a Diagnosis.

    The five log-derived checks (1-5) always run from the CloudWatch scans. The
    two admin-derived checks (6-7) run only when an admin token is present and a
    connection row came back; otherwise they degrade to skipped with a reason so
    a missing token never blocks the log checks. Results are returned in SOW
    order.
    """
    return Diagnosis(
        checks=[
            _check_deliveries_arriving(deliveries),
            _check_delivery_path(deliveries),
            _check_signatures_accepted(deliveries),
            _check_deliveries_producing_syncs(deliveries, syncs),
            _check_syncs_landing_rows(syncs),
            _check_connection_health(connection, token_present),
            _check_data_freshness(connection, token_present, now),
        ]
    )
