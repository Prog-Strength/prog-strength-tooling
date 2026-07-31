"""pst — the prog-strength tooling CLI entry point.

Resource-grouped: each subcommand group is a Typer sub-app mounted here, so
new probes/maintenance tasks (e.g. `pst chat`, `pst nutrition`) slot in
without touching existing commands. Single-shot commands (e.g. `status`) are
registered directly on the root app.
"""

from __future__ import annotations

import typer

from .commands import memory, status

# Typer collapses single newlines within a paragraph but preserves blank-line
# paragraph breaks, so each example is its own paragraph to keep it on one line.
HELP = """[bold]pst[/bold] — CLI tooling for Prog Strength Backend (API, Agent, MCP, DB).

Probe live services, inspect the agent's vector memory, and run routine \
maintenance against a chosen [bold]environment[/bold]. Commands default to \
[bold green]prod[/bold green]; pass [cyan]--env local[/cyan] (or set [cyan]PST_ENV[/cyan]) to \
target another.

[dim]Health checks need no auth; the memory commands need an admin token \
([cyan]PST_TOKEN[/cyan]).[/dim]"""

EPILOG = """[bold]Examples[/bold]

[cyan]pst status[/cyan] — are api, agent & mcp up, and on what versions?

[cyan]pst memory list --user ID[/cyan] — what the agent has stored about a user

[cyan]pst memory search --user ID --query "leg day"[/cyan] — what it would recall

[dim]Environments: prod (default) · local — select with --env or PST_ENV.[/dim]

[dim]Env vars: PST_ENV · PST_TOKEN (admin JWT, memory only) · PST_API_URL. \
Run 'pst COMMAND --help' for per-command options.[/dim]"""

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=HELP,
    epilog=EPILOG,
)

app.add_typer(memory.app, name="memory")
app.command("status")(status.status)


if __name__ == "__main__":
    app()
