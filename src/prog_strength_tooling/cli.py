"""pst — the prog-strength tooling CLI entry point.

Resource-grouped: each subcommand group is a Typer sub-app mounted here, so
new probes/maintenance tasks (e.g. `pst chat`, `pst nutrition`) slot in
without touching existing commands. Single-shot commands (e.g. `status`) are
registered directly on the root app.
"""

from __future__ import annotations

import typer

from .commands import memory, status

app = typer.Typer(
    no_args_is_help=True,
    help="Probing queries and maintenance tasks for the prog-strength stack.",
)

app.add_typer(memory.app, name="memory")
app.command("status")(status.status)


if __name__ == "__main__":
    app()
