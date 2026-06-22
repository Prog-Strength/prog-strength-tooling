"""`pst memory` — probe the agent vector memory stored per user.

Two commands:
  list    dump a user's stored memories (GET /admin/memories)
  search  retrieval probe — what the agent would recall (POST /admin/memories/search)

Both hit admin-gated endpoints; supply an admin JWT via --token or PST_TOKEN.
"""

from __future__ import annotations

import typer

from ..client import APIError, ClientError, MemoryClient
from ..config import resolve
from ..render import err_console, render_memories, render_search

app = typer.Typer(no_args_is_help=True, help="Probe agent vector memory.")

# Shared connection options. Declared once so both commands stay in sync.
ApiOption = typer.Option(
    None, "--api", envvar="PST_API_URL", help="API base URL (default http://localhost:8080)."
)
TokenOption = typer.Option(
    None, "--token", envvar="PST_TOKEN", help="Admin JWT.", show_default=False
)
JsonOption = typer.Option(False, "--json", help="Emit raw JSON instead of a table.")


def _fail(exc: Exception) -> None:
    """Print a one-line error to stderr and exit 1."""
    err_console.print(f"[red]error:[/red] {exc}")
    raise typer.Exit(code=1)


@app.command("list")
def list_memories(
    user: str = typer.Option(..., "--user", help="User id to inspect."),
    limit: int = typer.Option(100, "--limit", help="Max rows."),
    offset: int = typer.Option(0, "--offset", help="Row offset."),
    api: str | None = ApiOption,
    token: str | None = TokenOption,
    as_json: bool = JsonOption,
) -> None:
    """List the vector memories stored for a user."""
    cfg = resolve(api, token)
    try:
        with MemoryClient(cfg) as client:
            result = client.list_memories(user, limit=limit, offset=offset)
    except (ClientError, APIError) as exc:
        _fail(exc)
    render_memories(result, as_json=as_json)


@app.command("search")
def search(
    user: str = typer.Option(..., "--user", help="User id to probe."),
    query: str = typer.Option(..., "--query", help="Search query."),
    k: int | None = typer.Option(
        None, "--k", help="Max matches (omitted unless set; server applies default)."
    ),
    threshold: float | None = typer.Option(
        None,
        "--threshold",
        help="Distance cap (omitted unless set; 0 = full sweep, no cap).",
    ),
    api: str | None = ApiOption,
    token: str | None = TokenOption,
    as_json: bool = JsonOption,
) -> None:
    """Run a retrieval probe — the same path the agent uses to recall."""
    cfg = resolve(api, token)
    try:
        with MemoryClient(cfg) as client:
            result = client.search(user, query, k=k, threshold=threshold)
    except (ClientError, APIError) as exc:
        _fail(exc)
    render_search(result, as_json=as_json)
