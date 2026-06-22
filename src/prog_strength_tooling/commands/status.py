"""`pst status` — operational status and versions of the backend services.

Probes the api, agent, and mcp /health endpoints for the selected environment
so an operator can confirm at a glance that everything required is up and see
what version each is running. Health endpoints are public, so no token needed.
Exits non-zero if any service is down, so it can gate scripts.
"""

from __future__ import annotations

import typer

from ..config import DEFAULT_ENVIRONMENT, NAMED_ENVIRONMENTS, ConfigError, resolve_environment
from ..health import check_environment
from ..render import err_console, render_status

_env_names = ", ".join(sorted(NAMED_ENVIRONMENTS))


def status(
    env: str | None = typer.Option(
        None,
        "--env",
        help=f"Named environment: {_env_names} (or PST_ENV). Default: {DEFAULT_ENVIRONMENT}.",
        show_default=False,
    ),
    timeout: float = typer.Option(5.0, "--timeout", help="Per-service timeout, seconds."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
) -> None:
    """Check that the backend services are operational and report versions."""
    try:
        environment = resolve_environment(env)
    except ConfigError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)

    results = check_environment(environment, timeout)
    render_status(environment, results, as_json=as_json)

    # Non-zero exit if anything is down, so `pst status && deploy ...` works.
    if any(not r.up for r in results):
        raise typer.Exit(code=1)
