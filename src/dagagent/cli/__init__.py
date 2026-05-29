"""Typer CLI.

Entry point for ``dagagent`` command-line invocations. Hosts subcommands
for serving the REST gateway, running evals, managing jobs, and the
interactive onboard flow (Phase 4).
"""

from __future__ import annotations

import asyncio

import typer

from dagagent import __version__
from dagagent.gateways.tui import DEFAULT_BASE_URL, run_tui

app = typer.Typer(
    name="dagagent",
    help="A DAG-planning agent with deterministic tier routing.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed dagagent version."""
    typer.echo(__version__)


@app.command()
def tui(
    request: str = typer.Argument(..., help="The task to submit."),
    url: str = typer.Option(DEFAULT_BASE_URL, help="Base URL of a running gateway."),
) -> None:
    """Submit a task to a running gateway and watch it run in the terminal."""
    asyncio.run(run_tui(request, base_url=url))


if __name__ == "__main__":
    app()
