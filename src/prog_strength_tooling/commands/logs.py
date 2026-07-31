"""`pst logs` — find a request's server-side log lines by request id.

The workflow this exists for: the web or mobile client hits an error, the
response carries a `request_id` (in the error envelope body, and on the
`X-Request-ID` header), and you need the server side of that call. This
searches the CloudWatch log groups prog-strength-infra creates for the api,
agent, and mcp services and prints every matching line on one timeline.

All three services are searched by default. Not because a single id spans
them — the services accept an inbound X-Request-ID but don't forward one, so
an id lives in exactly one group today — but because the operator holding an
id from a client generally doesn't know which service minted it, and guessing
wrong looks identical to "not found".

Reads CloudWatch with your own AWS credentials, so no PST_TOKEN is needed.
"""

from __future__ import annotations

from datetime import datetime

import typer

from .. import cloudwatch
from ..config import DEFAULT_ENVIRONMENT, SERVICES, ConfigError, resolve_logs
from ..render import err_console, render_logs
from ..window import WindowError, resolve

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Find server-side logs in CloudWatch.",
)

#: Exit code for a config/credential/query failure, kept distinct from the
#: code for a successful search that found nothing — a script piping this
#: needs to tell "broken" apart from "no such request".
EXIT_ERROR = 2
EXIT_NOT_FOUND = 1

_services = ", ".join(SERVICES)


@app.command("trace")
def trace(
    request_id: str = typer.Argument(
        ...,
        metavar="REQUEST_ID",
        help="The request id from the client's error response or X-Request-ID header.",
    ),
    service: list[str] = typer.Option(
        None,
        "--service",
        "-s",
        help=f"Limit to one or more services ({_services}). Repeatable. Default: all.",
        show_default=False,
    ),
    since: str = typer.Option(
        None,
        "--since",
        help="Relative window ending now, e.g. 90m, 24h, 7d. Default: 24h.",
        show_default=False,
    ),
    start: datetime = typer.Option(
        None,
        "--start",
        help="Absolute window start (UTC), e.g. 2026-07-30T10:00:00.",
        show_default=False,
    ),
    end: datetime = typer.Option(
        None,
        "--end",
        help="Absolute window end (UTC). Defaults to now when --start is given.",
        show_default=False,
    ),
    limit: int = typer.Option(500, "--limit", help="Maximum log lines to display."),
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
    raw: bool = typer.Option(False, "--raw", help="Print original log lines, unparsed."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
) -> None:
    """Show every log line carrying a request id, across api, agent, and mcp.

    Searches the last 24 hours by default; widen with [cyan]--since 7d[/cyan]
    (CloudWatch retains 30 days). Uses your AWS credentials — no admin token.

    Exits 1 when the search succeeds but matches nothing, 2 on a
    configuration or AWS error.
    """
    try:
        cleaned_id = cloudwatch.validate_request_id(request_id)
        cfg = resolve_logs(env, service, profile, region)
        query_window = resolve(since, start, end)
    except (ConfigError, WindowError, cloudwatch.InvalidRequestIDError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=EXIT_ERROR)

    if limit < 1:
        err_console.print("[red]error:[/red] --limit must be at least 1.")
        raise typer.Exit(code=EXIT_ERROR)

    try:
        outcome = cloudwatch.search(cfg, cleaned_id, query_window, limit)
    except cloudwatch.CloudWatchError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=EXIT_ERROR)

    render_logs(
        outcome.records,
        request_id=cleaned_id,
        environment=cfg.environment,
        window_description=query_window.describe(),
        counts=outcome.counts,
        truncated=outcome.truncated,
        limit=limit,
        as_json=as_json,
        raw=raw,
    )

    if not outcome.records:
        raise typer.Exit(code=EXIT_NOT_FOUND)
