"""Typer CLI.

Entry point for ``dagagent`` command-line invocations. Hosts subcommands
for serving the REST gateway, running evals, managing jobs, and the
interactive onboard flow (Phase 4).
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="dagagent",
    help="A DAG-planning agent with deterministic tier routing.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed dagagent version."""
    from dagagent import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
