"""`pst whoop` — diagnose and repair WHOOP recovery ingestion.

Two commands:
  doctor   run the seven-check integration diagnosis and print findings + fixes
  resync   force a re-ingest of a user's recent recovery window (admin endpoint)

`doctor` is designed to work with *whatever* credentials the operator has. The
five log-derived checks (deliveries arriving, on the served path, with an
accepted signature, producing syncs, landing rows) read CloudWatch with the
operator's own AWS creds — no admin token needed, and all five are served from
a single capped pass over the api log group (`whooplogs.scan`). The two
admin-derived checks (connection health, data freshness) need an admin JWT AND
a `--user`; when either is absent they degrade to *skipped* rather than
failing, so a missing `PST_TOKEN` never blocks the log diagnosis. This mirrors
how the original outage was actually found: from the api's request log alone.

`resync` hits an admin-gated endpoint, so it resolves through
`config.resolve_admin` (a missing token stops it up front with instructions,
the same as the memory commands) rather than surfacing as a 401 from the server.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer

from .. import cloudwatch, whoop, whooplogs
from ..client import APIError, ClientError, WhoopAdminClient
from ..config import (
    DEFAULT_ENVIRONMENT,
    ConfigError,
    MissingTokenError,
    resolve_admin,
    resolve_logs,
)
from ..render import err_console, render_diagnosis, render_missing_token, render_resync
from ..window import WindowError, resolve

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Diagnose and repair WHOOP ingestion.",
)

#: Exit codes carry the same meaning as `pst logs`: a config/credential/AWS/API
#: failure (2) is kept distinct from a successful diagnosis that turned up
#: findings (1), so a script can tell "broken tooling" apart from "broken
#: integration". 0 = ran and healthy.
EXIT_ERROR = 2
EXIT_FINDINGS = 1

#: doctor's default window. Wider than `pst logs`' 24h because a WHOOP recovery
#: is scored roughly once a day: a day-long window can legitimately hold zero
#: deliveries on a healthy account, so 7d gives the delivery checks something to
#: judge (CloudWatch retains 30 days, so this stays well inside retention).
DEFAULT_SINCE = "7d"


@app.command("doctor")
def doctor(
    user: str = typer.Option(
        None,
        "--user",
        help="User id — enables the admin connection + freshness checks.",
        show_default=False,
    ),
    since: str = typer.Option(
        None,
        "--since",
        help=f"Relative log window ending now, e.g. 24h, 7d. Default: {DEFAULT_SINCE}.",
        show_default=False,
    ),
    max_events: int = typer.Option(
        whooplogs.MAX_EVENTS,
        "--max-events",
        help="Stop the CloudWatch scan after this many events; 0 scans the whole window.",
    ),
    env: str = typer.Option(
        None,
        "--env",
        help=f"Named environment (or PST_ENV). Default: {DEFAULT_ENVIRONMENT}.",
        show_default=False,
    ),
    profile: str = typer.Option(
        None,
        "--profile",
        help="AWS named profile (or PST_AWS_PROFILE). Default: the AWS default chain.",
        show_default=False,
    ),
    region: str = typer.Option(
        None,
        "--region",
        help="Override the AWS region holding the log groups.",
        show_default=False,
    ),
    token: str = typer.Option(
        None,
        "--token",
        help="Admin JWT (or PST_TOKEN). Optional: enables checks 6-7.",
        show_default=False,
    ),
    api: str = typer.Option(
        None,
        "--api",
        help="Explicit API base URL, overrides --env (or PST_API_URL).",
        show_default=False,
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
) -> None:
    """Run the seven-check WHOOP ingestion diagnosis over the recent window.

    The five log-derived checks always run from CloudWatch (your AWS creds). The
    connection + freshness checks run only with an admin token AND [cyan]--user[/cyan];
    otherwise they're skipped, not failed — a missing token never blocks the log
    diagnosis.

    Exits 0 when healthy, 1 when it found problems, 2 on a config or AWS error.
    """
    if max_events < 0:
        err_console.print("[red]error:[/red] --max-events must be 0 or greater.")
        raise typer.Exit(code=EXIT_ERROR)

    try:
        # The log checks are non-negotiable, so their config/window errors are
        # fatal (exit 2) — same shape as logs.py.
        # Only the api group is ever scanned (whooplogs.scan reads it alone), so
        # name it explicitly rather than letting resolve_logs default to all
        # three — otherwise the config line advertises agent and mcp too, and
        # contradicts the "scanning /prog-strength/api" line right below it.
        cfg_logs = resolve_logs(env, ("api",), profile, region)
        window = resolve(since or DEFAULT_SINCE, None, None)
        scan = whooplogs.scan(cfg_logs, window, max_events)
    except (ConfigError, WindowError, cloudwatch.CloudWatchError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=EXIT_ERROR)

    # Admin side degrades: a missing token is NOT fatal, it just skips 6/7.
    connection = None
    token_present = False
    try:
        cfg_admin = resolve_admin(api, token, env)
        token_present = True
        # Connection + freshness are per-user; without a --user there is no row
        # to fetch, so the checks skip (token_present stays True but connection
        # is None, which the engine treats as "skipped").
        if user:
            with WhoopAdminClient(cfg_admin) as client:
                try:
                    connection = client.get_connection(user)
                except APIError as exc:
                    if exc.status_code == 404:
                        # No connection for this user — leave connection None so
                        # checks 6/7 skip with a note rather than crashing.
                        err_console.print(
                            f"[yellow]note:[/yellow] no WHOOP connection for user "
                            f"{user!r}; connection + freshness checks skipped."
                        )
                    else:
                        raise
    except MissingTokenError:
        # Expected in the common case (log-only run): degrade silently, the
        # skipped checks already explain themselves in the output.
        pass
    except (ConfigError, ClientError, APIError) as exc:
        # A resolved token that then failed against the admin API is a real
        # error the operator should see — but it must not lose the log
        # diagnosis, so warn and continue with the admin checks skipped.
        err_console.print(
            f"[yellow]note:[/yellow] admin API check unavailable ({exc}); "
            f"connection + freshness checks skipped."
        )

    diagnosis = whoop.diagnose(
        scan.deliveries, scan.syncs, connection, token_present, now=datetime.now(timezone.utc)
    )
    render_diagnosis(diagnosis, scan, as_json=as_json)

    if not diagnosis.healthy:
        raise typer.Exit(code=EXIT_FINDINGS)


@app.command("resync")
def resync(
    user: str = typer.Option(..., "--user", help="User id to resync."),
    days: int = typer.Option(30, "--days", help="Recovery window to re-ingest, in days."),
    env: str = typer.Option(
        None,
        "--env",
        help=f"Named environment (or PST_ENV). Default: {DEFAULT_ENVIRONMENT}.",
        show_default=False,
    ),
    token: str = typer.Option(
        None,
        "--token",
        help="Admin JWT — required (or PST_TOKEN).",
        show_default=False,
    ),
    api: str = typer.Option(
        None,
        "--api",
        help="Explicit API base URL, overrides --env (or PST_API_URL).",
        show_default=False,
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
) -> None:
    """Force a re-ingest of a user's recent recovery window (admin endpoint).

    The CLI forwards [cyan]--days[/cyan] verbatim; the API clamps it server-side.
    Exits non-zero on a missing token, an API error, or a config error.
    """
    try:
        cfg = resolve_admin(api, token, env)
    except MissingTokenError as exc:
        # The one error an operator hits cold: full panel with how to get a
        # token, mirroring memory.py's _fail.
        render_missing_token(str(exc))
        raise typer.Exit(code=EXIT_FINDINGS)
    except ConfigError as exc:
        # An unknown env is a config error, not a "not found" — exit 2.
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=EXIT_ERROR)

    try:
        with WhoopAdminClient(cfg) as client:
            outcome = client.resync(user, days)
    except (ClientError, APIError) as exc:
        # 404/409/502 etc. — a clear one-liner and a non-zero exit distinct
        # from the config-error code.
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=EXIT_FINDINGS)

    render_resync(outcome, as_json=as_json)
