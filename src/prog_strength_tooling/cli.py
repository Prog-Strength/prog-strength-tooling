"""pst — the prog-strength tooling CLI entry point.

Resource-grouped: each subcommand group is a Typer sub-app mounted here, so
new probes/maintenance tasks (e.g. `pst chat`, `pst nutrition`) slot in
without touching existing commands. Single-shot commands (e.g. `status`) are
registered directly on the root app.
"""

from __future__ import annotations

import platform
import sys

import typer

from . import __version__, logsetup
from .commands import logs, memory, status, whoop

# Typer collapses single newlines within a paragraph but preserves blank-line
# paragraph breaks, so each example is its own paragraph to keep it on one line.
HELP = """[bold]pst[/bold] — CLI tooling for Prog Strength Backend (API, Agent, MCP, DB).

Probe live services, inspect the agent's vector memory, and run routine \
maintenance against a chosen [bold]environment[/bold]. Commands default to \
[bold green]prod[/bold green]; pass [cyan]--env local[/cyan] (or set [cyan]PST_ENV[/cyan]) to \
target another.

[dim]Health checks need no auth; the memory commands need an admin token \
([cyan]PST_TOKEN[/cyan]); the log commands use your AWS credentials.[/dim]"""

EPILOG = """[bold]Examples[/bold]

[cyan]pst status[/cyan] — are api, agent & mcp up, and on what versions?

[cyan]pst memory list --user ID[/cyan] — what the agent has stored about a user

[cyan]pst memory search --user ID --query "leg day"[/cyan] — what it would recall

[cyan]pst logs trace REQUEST_ID[/cyan] — server-side logs for a client's failed call

[dim]Environments: prod (default) · local — select with --env or PST_ENV.[/dim]

[dim]Env vars: PST_ENV · PST_TOKEN (admin JWT, memory only) · PST_API_URL · \
PST_AWS_PROFILE (logs only) · PST_LOG_LEVEL (debug/info/warning/error). \
Run 'pst COMMAND --help' for per-command options.[/dim]"""

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=HELP,
    epilog=EPILOG,
)


@app.callback(help=HELP)
def main(
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="More log detail: [cyan]-v[/cyan] for pst debug logs, "
        "[cyan]-vv[/cyan] to add AWS/HTTP wire logs.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Log only warnings and errors.",
    ),
) -> None:
    """Configure logging before any subcommand runs.

    Logs go to stderr at INFO by default, so `--json` on stdout stays pipeable
    and a long-running command still shows what phase it is in. Set
    PST_LOG_LEVEL instead of passing a flag every time; an explicit flag wins.
    """
    logsetup.configure(verbosity=verbose, quiet=quiet)
    log = logsetup.get_logger(__name__)
    log.debug(
        "pst starting %s",
        logsetup.kv(version=__version__, python=platform.python_version()),
    )
    log.debug("argv: %s", " ".join(logsetup.redact_argv(sys.argv[1:])))


app.add_typer(memory.app, name="memory")
app.add_typer(logs.app, name="logs")
app.add_typer(whoop.app, name="whoop")
app.command("status")(status.status)


if __name__ == "__main__":
    app()
