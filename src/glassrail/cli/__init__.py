"""Typer CLI.

Entry point for ``glassrail`` command-line invocations. Hosts subcommands
for serving the REST gateway, running tasks headlessly (``run``), managing
jobs, and the interactive onboard flow (Phase 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import typer
import uvicorn

from glassrail import __version__
from glassrail.config import Settings, get_settings
from glassrail.core import (
    ExecutionState,
    Node,
    NodeType,
    Plan,
    TaskStatus,
    new_task_id,
)
from glassrail.gateways.acp import run_acp
from glassrail.gateways.tui import DEFAULT_BASE_URL, run_tui
from glassrail.harness.builtin import register_eval_tools
from glassrail.runtime import build_runtime
from glassrail.validator import PlanValidator, topo_sort

log = logging.getLogger(__name__)

app = typer.Typer(
    name="glassrail",
    help="A DAG-planning agent with deterministic tier routing.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the installed glassrail version."""
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
def acp(
    fast: bool = typer.Option(
        False,
        "--fast",
        help="Route all tiers through OpenRouter instead of local models. "
        "Requires OPENROUTER_API_KEY or fast.api_key in config.",
    ),
) -> None:
    """Speak the Agent Client Protocol over stdio (for the Rust TUI / ACP clients).

    A long-running JSON-RPC 2.0 process: stdin/stdout carry the protocol,
    diagnostics go to stderr. A client (e.g. the ``clients/tui`` binary) spawns
    this as a subprocess, submits tasks via ``session/prompt``, and watches the
    plan and node execution stream back as ``session/update`` notifications.
    """
    asyncio.run(run_acp(fast_mode=fast))


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Interface to bind. Use 0.0.0.0 only when intentionally exposing the gateway.",
    ),
    port: int = typer.Option(8000, "--port", help="Port to bind."),
    reload: bool = typer.Option(False, "--reload", help="Reload the server on source changes."),
) -> None:
    """Serve the REST gateway with uvicorn."""
    uvicorn.run("glassrail.gateways.rest:app", host=host, port=port, reload=reload)


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
    fast: bool = typer.Option(
        False,
        "--fast",
        help="Route all tiers through OpenRouter instead of local models. "
        "Requires OPENROUTER_API_KEY or fast.api_key in config.",
    ),
) -> None:
    """Run a task end to end in-process and print its result.

    With ``--json`` the sole stdout line is a result envelope — final output,
    a normalized trajectory (nodes, tiers, branch decisions), status, and token
    count — which the eval framework's ``glassrail-cli`` backend consumes.
    """
    envelope = asyncio.run(
        _run_task(request, model=model, confirm=confirm, timeout_s=timeout, fast=fast)
    )
    if json_output:
        typer.echo(json.dumps(envelope))
    else:
        typer.echo(envelope.get("result") or envelope.get("error") or "(no output)")
    _exit_if_error(envelope)


# ── headless run plumbing ────────────────────────────────────────────────────


def _settings_for_run(*, model: str | None, confirm: bool, fast: bool) -> Settings:
    settings = get_settings()
    if fast:
        settings = settings.with_fast_mode()
    updates: dict[str, object] = {}
    if not confirm:
        updates["confirm_plans"] = False
    if model:
        updates["tier0"] = settings.tier0.model_copy(update={"model": model})
    return settings.model_copy(update=updates) if updates else settings


async def _run_task(
    request: str, *, model: str | None, confirm: bool, timeout_s: float | None, fast: bool
) -> dict[str, object]:
    settings = _settings_for_run(model=model, confirm=confirm, fast=fast)
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
        raw_out = result.output if result else None
        out_str: str | None = None
        if raw_out is not None:
            s = json.dumps(raw_out) if isinstance(raw_out, (dict, list)) else str(raw_out)
            out_str = s[:2048] if len(s) > 2048 else s
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
                "args_used": result.args_used if result else None,
                "output": out_str,
            }
        )
    return steps


def _envelope(state: ExecutionState, *, error: str | None = None) -> dict[str, object]:
    status = state.status.value
    is_error = (
        state.status
        in (
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
        )
        or error is not None
    )
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


def _exit_if_error(envelope: dict[str, object]) -> None:
    terminal_error_statuses = {
        TaskStatus.FAILED.value,
        TaskStatus.REJECTED.value,
        TaskStatus.CANCELLED.value,
    }
    if envelope.get("is_error") is True or envelope.get("status") in terminal_error_statuses:
        raise typer.Exit(code=1)


@app.command("exec-plan")
def exec_plan(
    plan_file: str = typer.Argument(..., help="Path to the plan JSON file."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit a JSON result envelope on stdout."
    ),
    no_validate: bool = typer.Option(
        False, "--no-validate", help="Skip plan validation (for negative harness tests)."
    ),
) -> None:
    """Execute a fixed plan JSON, bypassing the planner.

    Reads a plan from a JSON file, optionally validates it, then runs only the
    executor.  Emits the same ``--json`` envelope as ``run``.  Used by the
    harness-mechanics eval suite to inject deterministic plans without running
    the planner.
    """
    # Read the plan file synchronously before entering the event loop — this is
    # a one-shot CLI command, not a server, so blocking I/O is fine here.
    try:
        raw = Path(plan_file).read_text(encoding="utf-8")
        plan_data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not load plan file: {exc}"
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "result": "",
                        "trajectory": [],
                        "status": "failed",
                        "is_error": True,
                        "error": msg,
                        "total_cost_usd": None,
                        "total_tokens": 0,
                    }
                )
            )
        else:
            typer.echo(msg, err=True)
        raise typer.Exit(code=2) from exc

    envelope = asyncio.run(_exec_plan(plan_data, no_validate=no_validate))
    if json_output:
        typer.echo(json.dumps(envelope))
    else:
        typer.echo(envelope.get("result") or envelope.get("error") or "(no output)")
    _exit_if_error(envelope)


def _topo_sort_recursive(plan: Plan) -> None:
    """Populate sorted_node_ids on plan and all nested subplans, best-effort.

    Used by the --no-validate path so the executor can iterate nodes even when
    full structural validation is skipped. Errors are silently ignored — invalid
    deps or cycles in negative-test fixtures are intentional and will surface as
    executor failures instead.
    """
    with contextlib.suppress(Exception):
        plan.sorted_node_ids = topo_sort(plan)
    for node in plan.nodes:
        if node.subplan is not None:
            _topo_sort_recursive(node.subplan)


async def _exec_plan(plan_data: object, *, no_validate: bool) -> dict[str, object]:
    settings = get_settings()
    settings = settings.model_copy(update={"confirm_plans": False})
    rt = build_runtime(settings)
    register_eval_tools(rt.harness)

    try:
        plan = Plan.model_validate(plan_data)
    except Exception as exc:
        return {
            "result": "",
            "trajectory": [],
            "status": "failed",
            "is_error": True,
            "error": f"plan parse failed: {exc}",
            "total_cost_usd": None,
            "total_tokens": 0,
        }

    if not no_validate:
        validator = PlanValidator(harness=rt.harness, settings=settings)
        try:
            plan.sorted_node_ids = validator.validate(plan)
        except Exception as exc:
            return {
                "result": "",
                "trajectory": [],
                "status": "failed",
                "is_error": True,
                "error": f"plan validation failed: {exc}",
                "total_cost_usd": None,
                "total_tokens": 0,
            }
    else:
        # Skipping full validation (negative harness tests), but the executor
        # still needs sorted_node_ids to iterate nodes. Topo-sort the plan and
        # any nested subplans without structural checks; ignore errors (invalid
        # deps are intentional in some fixtures and will surface as executor
        # failures instead).
        _topo_sort_recursive(plan)

    state = ExecutionState(task_id=new_task_id(), user_request="<exec-plan>")
    state.plan = plan
    await rt.store.save_task(state)

    run_error: str | None = None
    try:
        await rt.orchestrator.execute_plan(state)
    except TimeoutError:
        run_error = "timed out"

    final = await rt.store.load_task(state.task_id) or state
    return _envelope(final, error=run_error)


if __name__ == "__main__":
    app()
