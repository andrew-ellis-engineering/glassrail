"""Eval harness — score the planner + executor against fixtures.

This is the machinery behind ``uv run pytest -m eval``. A :class:`Scenario`
pairs a user request with the canned LLM responses that drive a deterministic
run and the :class:`Expectations` the run is graded against. Scoring is split
into a *planning* dimension (was a sensible plan produced?) and an *execution*
dimension (did running it reach the right outcome?).

The scorer is provider-agnostic: :func:`run_scenario` takes an optional
``router``. With none, it builds a deterministic router from the scenario's
script; pass a real :class:`~dagagent.providers.TierRouter` and the very same
expectations grade a live model (see ``test_eval_suite.py``). The deterministic
path is what CI runs — reliable and offline; the live path is the opt-in
quality measurement that "may hit external services".
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from pytest import StashKey

from dagagent.config import Settings
from dagagent.core import (
    ExecutionState,
    NodeStatus,
    NodeType,
    TaskStatus,
    new_task_id,
)
from dagagent.executor import Executor, Orchestrator
from dagagent.harness import ToolHarness, register_builtins
from dagagent.planner import Planner
from dagagent.providers import Chunk, Message, TierRouter
from dagagent.state import InMemoryStateStore
from dagagent.validator import PlanValidator

# Stash keys the eval tests append results to and the terminal-summary hook
# reads. Defined here so test module and conftest share the same objects.
EVAL_RESULTS_KEY: StashKey[list[ScenarioResult]] = StashKey()
EVAL_LIVE_RESULTS_KEY: StashKey[list[ScenarioResult]] = StashKey()

PLANNING = "planning"
EXECUTION = "execution"


# ── Fixture model ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Expectations:
    """What a graded run should look like. Only the fields you set are scored.

    Every set field becomes one weighted check; the scenario's score is the
    fraction of its checks that pass. This keeps the suite declarative — a
    fixture states what matters and the scorer emits exactly those checks.
    """

    status: TaskStatus = TaskStatus.COMPLETED
    """The task's terminal status."""
    must_validate: bool = True
    """Planning must yield a validated plan (``state.plan is not None``)."""
    min_nodes: int | None = None
    max_nodes: int | None = None
    node_types: tuple[NodeType, ...] = ()
    """Node types that must all appear in the plan."""
    tools: tuple[str, ...] = ()
    """Tool names the plan must reference (top-level only)."""
    final_output_contains: tuple[str, ...] = ()
    """Case-insensitive substrings the final output must contain."""
    branches: tuple[tuple[int, str], ...] = ()
    """``(decision_node_id, branch_taken)`` pairs the run must record."""
    no_failed_nodes: bool = True
    """No node result may carry ``FAILED`` (only checked when expecting COMPLETED)."""


@dataclass(frozen=True)
class Scenario:
    """One eval case: a request, the canned responses, and the grading rubric."""

    id: str
    request: str
    script: tuple[str, ...]
    """Canned LLM responses, consumed in the order the engine makes calls.

    Ignored when :func:`run_scenario` is handed a live router.
    """
    expect: Expectations
    settings: Settings | None = None
    pass_threshold: float = 1.0
    """Minimum overall score for the scenario to count as passed."""
    description: str = ""
    deterministic_only: bool = False
    """Skip in live runs — its expected outcome depends on the canned script
    (e.g. a forced planning failure a real model wouldn't reproduce)."""


# ── Result model ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    name: str
    dimension: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    scenario_id: str
    checks: list[CheckResult] = field(default_factory=list)
    status: TaskStatus | None = None
    error: str | None = None
    leftover_script: int = 0
    threshold: float = 1.0

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(1 for c in self.checks if c.passed) / len(self.checks)

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold

    def dimension_score(self, dimension: str) -> float | None:
        relevant = [c for c in self.checks if c.dimension == dimension]
        if not relevant:
            return None
        return sum(1 for c in relevant if c.passed) / len(relevant)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


# ── Deterministic provider ────────────────────────────────────────────────


class ScriptedProvider:
    """A provider that replays canned responses from a shared queue.

    Tier views over one queue let the :class:`TierRouter` route by tier while
    responses are still consumed in true global call order — a tier-2 ``think``
    node and a tier-0 synthesis pop from the same script.
    """

    def __init__(self, queue: deque[str], *, tier: int) -> None:
        self._queue = queue
        self._tier = tier

    @property
    def name(self) -> str:
        return f"scripted-t{self._tier}"

    @property
    def tier(self) -> int:
        return self._tier

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> AsyncIterator[Chunk]:
        del messages, json_mode, max_tokens, timeout_s
        if not self._queue:
            raise RuntimeError("scripted provider exhausted: fixture is missing a response")
        yield Chunk(text=self._queue.popleft(), tokens_used=1)


def make_scripted_router(script: Sequence[str]) -> tuple[TierRouter, deque[str]]:
    """Build a four-tier router over one shared response queue.

    Returns the router and the queue so a caller can assert the script was
    fully consumed (leftover responses mean the fixture is out of sync with
    the engine's call sequence).
    """
    queue: deque[str] = deque(script)
    providers = [ScriptedProvider(queue, tier=t) for t in range(4)]
    return TierRouter(providers), queue


def build_orchestrator(
    *, router: TierRouter, settings: Settings
) -> tuple[Orchestrator, InMemoryStateStore]:
    """Wire a real planner/executor/validator stack around ``router``."""
    harness = ToolHarness()
    register_builtins(harness)
    validator = PlanValidator(harness=harness, settings=settings)
    planner = Planner(router=router, harness=harness, validator=validator)
    executor = Executor(router=router, harness=harness, settings=settings)
    store = InMemoryStateStore()
    orchestrator = Orchestrator(
        planner=planner,
        executor=executor,
        state_store=store,
        settings=settings,
    )
    return orchestrator, store


# ── Scoring ───────────────────────────────────────────────────────────────


def score_run(
    state: ExecutionState, expect: Expectations, *, content: bool = True
) -> list[CheckResult]:
    """Grade a finished run against its expectations.

    Emits one :class:`CheckResult` per expectation that was set. The status
    check is always emitted; structural checks only when the plan exists.

    ``content=False`` drops the checks whose expected value is tied to the
    canned script — the exact final-output wording and branch taken. Live
    runs use it so a competent model is still graded on structure (plan
    validity, node count, node types, tools) without being marked down for
    phrasing the deterministic fixture happened to script.
    """
    checks: list[CheckResult] = [
        CheckResult(
            "status_matches",
            EXECUTION,
            state.status is expect.status,
            f"got {state.status}, expected {expect.status}",
        )
    ]

    plan = state.plan
    if expect.must_validate:
        checks.append(
            CheckResult(
                "plan_valid",
                PLANNING,
                plan is not None,
                "ok" if plan is not None else "no validated plan produced",
            )
        )

    if plan is not None:
        count = len(plan.nodes)
        if expect.min_nodes is not None or expect.max_nodes is not None:
            lo = expect.min_nodes if expect.min_nodes is not None else 0
            hi = expect.max_nodes if expect.max_nodes is not None else count
            checks.append(
                CheckResult(
                    "node_count",
                    PLANNING,
                    lo <= count <= hi,
                    f"{count} nodes (want {lo}..{hi})",
                )
            )
        if expect.node_types:
            present = {n.type for n in plan.nodes}
            missing = [t for t in expect.node_types if t not in present]
            checks.append(
                CheckResult(
                    "node_types_present",
                    PLANNING,
                    not missing,
                    "ok" if not missing else f"missing {missing}",
                )
            )
        if expect.tools:
            referenced = {n.tool for n in plan.nodes if n.type is NodeType.TOOL and n.tool}
            missing_tools = [t for t in expect.tools if t not in referenced]
            checks.append(
                CheckResult(
                    "tools_referenced",
                    PLANNING,
                    not missing_tools,
                    "ok" if not missing_tools else f"missing {missing_tools}",
                )
            )

    if content and expect.final_output_contains:
        out = (state.final_output or "").lower()
        missing_sub = [s for s in expect.final_output_contains if s.lower() not in out]
        checks.append(
            CheckResult(
                "final_output_contains",
                EXECUTION,
                not missing_sub,
                "ok" if not missing_sub else f"missing {missing_sub} in {state.final_output!r}",
            )
        )

    if content and expect.branches:
        recorded = {(e.node_id, e.branch_taken) for e in state.branch_log}
        unmet = [pair for pair in expect.branches if pair not in recorded]
        checks.append(
            CheckResult(
                "branches_taken",
                EXECUTION,
                not unmet,
                "ok" if not unmet else f"unmet {unmet}; recorded {sorted(recorded)}",
            )
        )

    if expect.no_failed_nodes and expect.status is TaskStatus.COMPLETED:
        failed = sorted(nid for nid, r in state.results.items() if r.status is NodeStatus.FAILED)
        checks.append(
            CheckResult(
                "no_failed_nodes",
                EXECUTION,
                not failed,
                "ok" if not failed else f"failed nodes {failed}",
            )
        )

    return checks


async def run_scenario(
    scenario: Scenario, *, router: TierRouter | None = None, content: bool = True
) -> ScenarioResult:
    """Run one scenario end to end and grade it.

    With no ``router`` the run is deterministic (scripted). Pass a live router
    to grade a real model against the same expectations; pair it with
    ``content=False`` so phrasing differences don't count against the model.
    """
    settings = scenario.settings or Settings()
    leftover: deque[str] = deque()
    if router is None:
        router, leftover = make_scripted_router(scenario.script)

    orchestrator, store = build_orchestrator(router=router, settings=settings)
    state = ExecutionState(task_id=new_task_id(), user_request=scenario.request)
    await store.save_task(state)
    await orchestrator.run(state.task_id)

    final = await store.load_task(state.task_id)
    assert final is not None, "store dropped the task mid-run"

    return ScenarioResult(
        scenario_id=scenario.id,
        checks=score_run(final, scenario.expect, content=content),
        status=final.status,
        error=final.error,
        leftover_script=len(leftover),
        threshold=scenario.pass_threshold,
    )


# ── Reporting ─────────────────────────────────────────────────────────────


def format_summary(results: Sequence[ScenarioResult], *, label: str = "deterministic") -> str:
    """Render the aggregate score table printed at the end of an eval run."""
    rule = "=" * 72
    lines = [
        "",
        rule,
        f"  EVAL SUMMARY ({label}) — {len(results)} scenarios",
        rule,
        f"  {'scenario':<30}{'plan':>6}{'exec':>6}{'score':>7}   result",
        f"  {'-' * 66}",
    ]
    for r in results:
        plan = r.dimension_score(PLANNING)
        execn = r.dimension_score(EXECUTION)
        plan_s = f"{plan:.2f}" if plan is not None else "  --"
        exec_s = f"{execn:.2f}" if execn is not None else "  --"
        verdict = "PASS" if r.passed else "FAIL"
        lines.append(f"  {r.scenario_id:<30}{plan_s:>6}{exec_s:>6}{r.score:>7.2f}   {verdict}")
        for c in r.failures():
            lines.append(f"        x [{c.dimension[:4]}] {c.name}: {c.detail}")

    passed = sum(1 for r in results if r.passed)
    mean = sum(r.score for r in results) / len(results) if results else 0.0
    lines += [
        f"  {'-' * 66}",
        f"  {passed}/{len(results)} passed   mean score {mean:.2f}",
        rule,
        "",
    ]
    return "\n".join(lines)
