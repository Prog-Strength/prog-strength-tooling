"""Rendering for command output: rich tables by default, raw JSON on --json.

Kept separate from the commands so the network/parse layer never touches a
console, and so rendering can be smoke-tested without a server.
"""

from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from .models import MemoryList, SearchResult

console = Console()
err_console = Console(stderr=True)


def _fmt_dt(value) -> str:
    """Compact UTC-ish timestamp for table cells."""
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def render_memories(result: MemoryList, *, as_json: bool) -> None:
    """Render a user's stored memories."""
    if as_json:
        _print_json(result)
        return

    if not result.memories:
        console.print("[dim]no stored memories for this user[/dim]")
        return

    table = Table(title=f"stored memories ({len(result.memories)})")
    table.add_column("created", style="cyan", no_wrap=True)
    table.add_column("distilled text", style="white", overflow="fold")
    table.add_column("session", style="dim", no_wrap=True)
    table.add_column("model", style="dim", no_wrap=True)
    table.add_column("dim", justify="right", style="dim")
    table.add_column("superseded", no_wrap=True)

    for m in result.memories:
        superseded = (
            f"[yellow]{_fmt_dt(m.superseded_at)}[/yellow]"
            if m.superseded_at
            else "[green]active[/green]"
        )
        table.add_row(
            _fmt_dt(m.created_at),
            m.distilled_text,
            m.source_session_id or "-",
            m.embedding_model or "-",
            str(m.embedding_dim),
            superseded,
        )
    console.print(table)


def render_search(result: SearchResult, *, as_json: bool) -> None:
    """Render a retrieval probe — header shows the active distance cap."""
    if as_json:
        _print_json(result)
        return

    console.print(
        f"[bold]retrieval probe[/bold]  active threshold (distance cap): "
        f"[magenta]{result.threshold}[/magenta]"
    )

    if not result.matches:
        console.print("[dim]no matches under the active threshold[/dim]")
        return

    table = Table(title=f"matches ({len(result.matches)})")
    table.add_column("distance", justify="right", style="magenta", no_wrap=True)
    table.add_column("text", style="white", overflow="fold")
    table.add_column("session", style="dim", no_wrap=True)
    table.add_column("created", style="cyan", no_wrap=True)

    for hit in result.matches:
        table.add_row(
            f"{hit.distance:.4f}",
            hit.text,
            hit.source_session_id or "-",
            _fmt_dt(hit.created_at),
        )
    console.print(table)


def _print_json(model) -> None:
    """Emit the model as raw, indented JSON for scripting/piping."""
    console.print_json(json.dumps(model.model_dump(mode="json")))
