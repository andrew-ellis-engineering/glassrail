"""Typer CLI.

Entry point for ``dagagent`` command-line invocations. Hosts subcommands
for serving the REST gateway, running tasks headlessly (``run``), managing
jobs, and the interactive onboard flow (Phase 4).
"""

from __future__ import annotations

import asyncio
import json

import typer

from dagagent import __version__
from dagagent.config import Settings, get_settings
from dagagent.core import (
    ExecutionState,
    Node,
    NodeType,
    TaskStatus,
    new_task_id,
)
from dagagent.gateways.tui import DEFAULT_BASE_URL, run_tui
from dagagent.runtime import build_runtime

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
    dag: bool = typer.Option(True, help="Show the live DAG view above the node table."),
) -> None:
    """Submit a task to a running gateway and watch it run in the terminal."""
    asyncio.run(run_tui(request, base_url=url, show_dag=dag))


@app.command()
def run(
    request: str = typer.Argument(..., help="The task to run."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a JSON result envelope on stdout (for eval harnesses)."
    ),
    model: str | None = typer.Option(None, "--model", help="Override tier 0's model for this run."),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Wall-clock budget for the run, in seconds."
    ),
    confirm: bool = typer.Option(
        False,
        "--confirm/--no-confirm",
        help="Honor the HITL confirmation gate (off by default for headless runs).",
    ),
) -> None:
    """Run a task end to end in-process and print its result.

    With ``--json`` the sole stdout line is a result envelope — final output,
    a normalized trajectory (nodes, tiers, branch decisions), status, and token
    count — which the eval framework's ``dagagent-cli`` backend consumes.
    """
    envelope = asyncio.run(_run_task(request, model=model, confirm=confirm, timeout_s=timeout))
    if json_output:
        typer.echo(json.dumps(envelope))
    else:
        typer.echo(envelope.get("result") or envelope.get("error") or "(no output)")


# ── headless run plumbing ────────────────────────────────────────────────────


def _settings_for_run(*, model: str | None, confirm: bool) -> Settings:
    settings = get_settings()
    updates: dict[str, object] = {}
    if not confirm:
        updates["confirm_plans"] = False
    if model:
        updates["tier0"] = settings.tier0.model_copy(update={"model": model})
    return settings.model_copy(update=updates) if updates else settings


async def _run_task(
    request: str, *, model: str | None, confirm: bool, timeout_s: float | None
) -> dict[str, object]:
    settings = _settings_for_run(model=model, confirm=confirm)
    rt = build_runtime(settings)
    state = ExecutionState(task_id=new_task_id(), user_request=request)
    await rt.store.save_task(state)

    run_error: str | None = None
    try:
        if timeout_s:
            await asyncio.wait_for(rt.orchestrator.run(state.task_id), timeout=timeout_s)
        else:
            await rt.orchestrator.run(state.task_id)
    except TimeoutError:
        run_error = "timed out"
        latest = await rt.store.load_task(state.task_id)
        timed_out = latest or state
        timed_out.status = TaskStatus.FAILED
        timed_out.error = run_error
        timed_out.touch()
        await rt.store.save_task(timed_out)

    final = await rt.store.load_task(state.task_id) or state
    return _envelope(final, error=run_error)


def _node_token(node: Node) -> str:
    """Stable trajectory token: the tool name for tool nodes, else the node type.

    Decision nodes are just ``decision`` regardless of the branch taken — branch
    labels are planner-chosen and unstable, so branch correctness is graded on
    the observable result text, not on this token. The branch still travels in
    each step's ``branch_taken`` field for inspection.
    """
    if node.type == NodeType.TOOL:
        return node.tool or "tool"
    return node.type.value


def _trajectory(state: ExecutionState) -> list[dict[str, object]]:
    if state.plan is None:
        return []
    nodes = {n.id: n for n in state.plan.nodes}
    order = state.plan.sorted_node_ids or [n.id for n in state.plan.nodes]
    steps: list[dict[str, object]] = []
    for node_id in order:
        node = nodes.get(node_id)
        if node is None:
            continue
        result = state.results.get(node_id)
        branch = result.branch_taken if result else None
        steps.append(
            {
                "tool": _node_token(node),
                "input": node.args_template or {},
                "node_id": node.id,
                "node_type": node.type.value,
                "tier_used": result.tier_used if result else None,
                "status": result.status.value if result else "pending",
                "confidence": result.confidence if result else 1.0,
                "flagged": result.flagged if result else False,
                "branch_taken": branch,
            }
        )
    return steps


def _envelope(state: ExecutionState, *, error: str | None = None) -> dict[str, object]:
    status = state.status.value
    is_error = state.status is TaskStatus.FAILED or error is not None
    total_tokens = sum(r.tokens_used for r in state.results.values())
    return {
        "result": state.final_output or "",
        "trajectory": _trajectory(state),
        "status": status,
        "is_error": is_error,
        "error": error or state.error,
        # Local/self-hosted inference has no per-call dollar cost; tokens are the
        # meaningful budget signal and travel in the envelope for the record.
        "total_cost_usd": None,
        "total_tokens": total_tokens,
        "task_id": str(state.task_id),
        "replan_count": state.replan_count,
        "plan": state.plan.model_dump(mode="json") if state.plan is not None else None,
        "planning_attempts": [
            attempt.model_dump(mode="json") for attempt in state.planning_attempts
        ],
        "branch_log": [e.model_dump(mode="json") for e in state.branch_log],
        "flagged_nodes": [r.node_id for r in state.results.values() if r.flagged],
    }


if __name__ == "__main__":
    app()
