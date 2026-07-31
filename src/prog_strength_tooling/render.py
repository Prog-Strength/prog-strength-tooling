"""Rendering for command output: rich tables by default, raw JSON on --json.

Kept separate from the commands so the network/parse layer never touches a
console, and so rendering can be smoke-tested without a server.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Environment
from .health import ServiceStatus
from .logparse import LogRecord
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


def render_missing_token(message: str) -> None:
    """Render the 'no admin token' error as a bordered, hard-to-miss block.

    A panel rather than a one-liner because this is the one error an operator
    hits cold — the message includes how to supply a token and how to get one,
    and it must survive being skimmed. Rendered as plain Text so JSON/JWT
    punctuation in the examples isn't read as rich markup.
    """
    err_console.print(
        Panel(
            Text(message),
            title="[bold red]missing admin token[/bold red]",
            title_align="left",
            border_style="red",
        ),
        highlight=False,
    )


def _print_json(model) -> None:
    """Emit the model as raw, indented JSON for scripting/piping."""
    console.print_json(json.dumps(model.model_dump(mode="json")))


def render_status(environment: Environment, results: list[ServiceStatus], *, as_json: bool) -> None:
    """Render the operational status of an environment's backend services."""
    if as_json:
        payload = {
            "environment": environment.name,
            "services": [asdict(r) for r in results],
        }
        console.print_json(json.dumps(payload))
        return

    up = sum(1 for r in results if r.up)
    title = f"backend status — {environment.name}  ({up}/{len(results)} up)"
    table = Table(title=title)
    table.add_column("service", style="cyan", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("version", style="white", no_wrap=True)
    table.add_column("latency", justify="right", style="dim", no_wrap=True)
    table.add_column("endpoint", style="dim", overflow="fold")

    for r in results:
        if r.up:
            status = "[green]UP[/green]"
        else:
            status = f"[red]DOWN[/red] — {r.detail}"
        latency = f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "-"
        table.add_row(r.name, status, r.version or "-", latency, r.url)
    console.print(table)


#: Level -> rich style for the logs table. Anything unlisted (including the
#: "-" of an unparsed line) renders unstyled.
_LEVEL_STYLES = {
    "CRITICAL": "bold red",
    "FATAL": "bold red",
    "ERROR": "red",
    "WARN": "yellow",
    "WARNING": "yellow",
    "INFO": "green",
    "DEBUG": "dim",
    "TRACE": "dim",
}


def _fmt_log_ts(value) -> str:
    """Millisecond-precision UTC stamp — ordering within a request matters."""
    return value.strftime("%m-%d %H:%M:%S.%f")[:-3]


def render_logs(
    records: list[LogRecord],
    *,
    request_id: str,
    environment: str,
    window_description: str,
    counts: dict[str, int],
    truncated: bool,
    limit: int,
    as_json: bool,
    raw: bool,
) -> None:
    """Render the merged, time-ordered timeline for one request id.

    Log bodies are rendered as rich Text, never markup: a log line containing
    something like "[/dim]" or an unbalanced bracket is ordinary content here
    and must not be interpreted as styling (or crash the render).
    """
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "request_id": request_id,
                    "environment": environment,
                    "window": window_description,
                    "counts": counts,
                    "truncated": truncated,
                    "records": [
                        {
                            "service": r.service,
                            "timestamp": r.timestamp.isoformat(),
                            "level": r.level,
                            "logger": r.logger,
                            "message": r.message,
                            "stream": r.stream,
                            "raw": r.raw,
                        }
                        for r in records
                    ],
                }
            )
        )
        return

    if not records:
        render_no_logs(
            request_id=request_id,
            searched=list(counts),
            window_description=window_description,
        )
        return

    breakdown = "  ".join(f"{service} {counts.get(service, 0)}" for service in counts)
    console.print(
        f"[bold]request_id[/bold] [magenta]{request_id}[/magenta]  "
        f"[dim]{environment} · {window_description} · {breakdown}[/dim]"
    )

    if raw:
        for r in records:
            line = Text()
            line.append(f"{_fmt_log_ts(r.timestamp)} ", style="dim")
            line.append(f"{r.service:<5} ", style="cyan")
            line.append(r.raw)
            console.print(line, highlight=False)
    else:
        table = Table(title=f"log lines ({len(records)})")
        table.add_column("time (UTC)", style="dim", no_wrap=True)
        table.add_column("service", style="cyan", no_wrap=True)
        table.add_column("level", no_wrap=True)
        table.add_column("message", style="white", overflow="fold")

        for r in records:
            body = Text()
            if r.logger:
                body.append(f"{r.logger} ", style="dim")
            body.append(r.message)
            table.add_row(
                _fmt_log_ts(r.timestamp),
                r.service,
                Text(r.level, style=_LEVEL_STYLES.get(r.level.upper(), "")),
                body,
            )
        console.print(table)

    if truncated:
        err_console.print(
            f"[yellow]note:[/yellow] output truncated at --limit {limit}; "
            f"more matching lines exist. Raise --limit or narrow the window."
        )


def render_no_logs(*, request_id: str, searched: list[str], window_description: str) -> None:
    """Explain an empty result and what to try next.

    A bare 'no results' can't distinguish 'too narrow a window' from 'wrong
    service' from 'mistyped id' — and all three are common enough that the
    operator shouldn't have to guess which one they hit.
    """
    groups = ", ".join(searched) if searched else "any service"
    console.print(
        f"[dim]no lines matching[/dim] [magenta]{request_id}[/magenta] "
        f"[dim]in {groups} over the {window_description}[/dim]"
    )
    console.print(
        "\n  · widen the window — [cyan]--since 7d[/cyan] "
        "[dim](CloudWatch retains 30 days)[/dim]"
        "\n  · request ids don't propagate between services: only the service the "
        "client\n    actually called will have the id"
        "\n  · confirm the id was copied whole from the response body's "
        "[cyan]request_id[/cyan]\n    or the [cyan]X-Request-ID[/cyan] header"
    )
