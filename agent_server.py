#!/usr/bin/env python3
"""
Tiered Planning Agent Server
─────────────────────────────
DAG-based execution engine with:
  • Local (Rapid-MLX) → cloud tier routing with timeout fallthrough
  • Structured plan generation with decision/branch nodes
  • Tool harness with schema validation
  • Full edge case coverage (cycles, empty results, nesting limits, etc.)
  • Serialisable state for resume after failure
  • Branch decision log for debugging silent wrong-branch cascades

Install deps:
    pip install fastapi uvicorn httpx pydantic

Run:
    python agent_server.py

API:
    POST /task                  Submit a task
    GET  /task/{id}             Poll status / results
    POST /task/{id}/resume      Resume a paused task
    GET  /tools                 List registered tools
    GET  /task/{id}/branch-log  Debug: show every branch decision taken
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent")

# ─── Settings ─────────────────────────────────────────────────────────────────

class Settings(BaseModel):
    # Tier 0 — local Rapid-MLX
    tier0_url: str = "http://localhost:8080/v1"
    tier0_model: str = "qwen3.6-35b-moe"
    tier0_timeout: float = 10.0          # seconds before falling to tier 1

    # Tier 1-3 — OpenRouter
    openrouter_api_key: str = ""         # set via env: OPENROUTER_API_KEY
    tier1_url: str = "https://openrouter.ai/api/v1"
    tier1_model: str = "deepseek/deepseek-v4-flash"
    tier2_url: str = "https://openrouter.ai/api/v1"
    tier2_model: str = "deepseek/deepseek-v4-pro"
    tier3_url: str = "https://openrouter.ai/api/v1"
    tier3_model: str = "anthropic/claude-sonnet-4-6"

    # Execution limits
    max_plan_steps: int = 12
    max_decision_nesting_depth: int = 2   # cap to keep graph debuggable
    max_step_output_tokens: int = 2000    # per-step output cap
    max_replan_attempts: int = 1
    confidence_threshold: float = 0.75   # below this → flag output
    local_timeout_s: float = 10.0        # tier 0 timeout
    confirm_plans: bool = False           # True → pause for user confirmation

    @classmethod
    def from_env(cls) -> "Settings":
        import os
        return cls(openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""))


settings = Settings.from_env()

# ─── Domain Models ────────────────────────────────────────────────────────────

class StepType(str, Enum):
    TOOL = "tool"
    DECISION = "decision"
    SYNTHESIS = "synthesis"


class PlanStep(BaseModel):
    id: int
    type: StepType
    description: str
    tool: Optional[str] = None           # TOOL steps only
    args_template: Optional[Dict] = None # static args; dynamic args filled from context
    context_needed: List[int] = Field(default_factory=list)  # step IDs whose output this needs
    # DECISION step fields
    condition: Optional[str] = None
    branches: Optional[Dict[str, List[int]]] = None  # {"yes": [3,4], "no": [5]}
    default_branch: Optional[str] = None
    # Routing hints (set by planner, overrideable)
    reasoning_required: bool = False
    forced_tier: Optional[int] = None    # None = auto-route


class Plan(BaseModel):
    steps: List[PlanStep]
    # Populated by validator
    sorted_step_ids: List[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan(self) -> "Plan":
        # Populated after validation; actual checks happen in PlanValidator
        return self


class StepStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    EMPTY = "empty"         # tool returned nothing — not an error, but notable


class StepResult(BaseModel):
    step_id: int
    status: StepStatus
    output: Any = None
    branch_taken: Optional[str] = None
    confidence: float = 1.0
    flagged: bool = False               # True if confidence < threshold
    tokens_used: int = 0
    execution_time_s: float = 0.0
    error: Optional[str] = None
    tier_used: Optional[int] = None


class TaskStatus(str, Enum):
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"                   # mid-execution pause for resume


class ExecutionState(BaseModel):
    task_id: str
    user_request: str
    plan: Optional[Plan] = None
    results: Dict[int, StepResult] = Field(default_factory=dict)
    completed_steps: List[int] = Field(default_factory=list)
    skipped_steps: List[int] = Field(default_factory=list)
    branch_log: List[Dict] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PLANNING
    replan_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    final_output: Optional[str] = None
    error: Optional[str] = None

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


# ─── In-Memory Task Store ─────────────────────────────────────────────────────
# Replace with Redis / SQLite for persistence

_tasks: Dict[str, ExecutionState] = {}


# ─── Tool Harness ─────────────────────────────────────────────────────────────

class ToolRegistrationError(Exception):
    pass


class ToolExecutionError(Exception):
    pass


class ToolHarness:
    """
    Registry for callable tools. Supports decorator-style registration.

    Usage:
        harness = ToolHarness()

        @harness.register(
            name="calendar_get",
            description="Fetch calendar events for a date range",
            parameters={
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "ISO date, e.g. 2026-05-24"},
                },
                "required": ["date"],
            },
        )
        async def calendar_get(date: str) -> dict:
            ...
    """

    def __init__(self) -> None:
        self._funcs: Dict[str, Callable] = {}
        self._schemas: Dict[str, Dict] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: Dict,
    ) -> Callable:
        """Decorator factory."""
        def decorator(func: Callable) -> Callable:
            if name in self._funcs:
                raise ToolRegistrationError(f"Tool '{name}' already registered")
            self._funcs[name] = func
            self._schemas[name] = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
            log.info("Registered tool: %s", name)
            return func
        return decorator

    def schema_for(self, name: str) -> Optional[Dict]:
        return self._schemas.get(name)

    def all_schemas(self) -> List[Dict]:
        return list(self._schemas.values())

    def all_names(self) -> Set[str]:
        return set(self._funcs.keys())

    def validate_names(self, names: List[str]) -> List[str]:
        """Return list of names that are NOT registered."""
        return [n for n in names if n not in self._funcs]

    async def execute(self, name: str, args: Dict) -> Any:
        if name not in self._funcs:
            raise ToolExecutionError(f"Unknown tool: '{name}'")
        try:
            func = self._funcs[name]
            if asyncio.iscoroutinefunction(func):
                return await func(**args)
            else:
                return func(**args)
        except Exception as exc:
            raise ToolExecutionError(f"Tool '{name}' raised: {exc}") from exc


harness = ToolHarness()

# ─── Example Tool Registrations ───────────────────────────────────────────────
# Replace stubs with real implementations

@harness.register(
    name="calendar_get",
    description="Fetch calendar events for a given date",
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
        },
        "required": ["date"],
    },
)
async def _calendar_get(date: str) -> Dict:
    # Stub — replace with real calendar MCP call
    return {"date": date, "events": [], "source": "stub"}


@harness.register(
    name="memory_search",
    description="Search the agent's long-term memory store",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
)
async def _memory_search(query: str, limit: int = 5) -> Dict:
    return {"query": query, "results": [], "source": "stub"}


@harness.register(
    name="web_search",
    description="Search the web for current information",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    },
)
async def _web_search(query: str) -> Dict:
    return {"query": query, "results": [], "source": "stub"}


@harness.register(
    name="file_read",
    description="Read a file from the local filesystem",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
    },
)
async def _file_read(path: str) -> Dict:
    try:
        import aiofiles  # type: ignore
        async with aiofiles.open(path) as f:
            content = await f.read()
        return {"path": path, "content": content}
    except ImportError:
        with open(path) as f:
            return {"path": path, "content": f.read()}
    except Exception as exc:
        return {"path": path, "error": str(exc)}


# ─── LLM Client with Tier Routing ─────────────────────────────────────────────

class LLMClient:
    """
    OpenAI-compatible client with tier routing.
    Tier 0 (local) has a hard timeout; on timeout it falls through to tier 1.
    """

    TIERS = {
        0: lambda: (settings.tier0_url, settings.tier0_model, None),
        1: lambda: (settings.tier1_url, settings.tier1_model, settings.openrouter_api_key),
        2: lambda: (settings.tier2_url, settings.tier2_model, settings.openrouter_api_key),
        3: lambda: (settings.tier3_url, settings.tier3_model, settings.openrouter_api_key),
    }

    async def complete(
        self,
        messages: List[Dict],
        tier: int = 0,
        tools: Optional[List[Dict]] = None,
        json_mode: bool = False,
        max_tokens: int = 1024,
    ) -> Tuple[str, int, int]:
        """
        Returns (content, tier_actually_used, tokens_used).
        Falls through tier 0 → 1 on timeout.
        """
        start_tier = tier
        while tier <= 3:
            base_url, model, api_key = self.TIERS[tier]()
            timeout = settings.local_timeout_s if tier == 0 else 60.0
            try:
                content, tokens = await self._call(
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    messages=messages,
                    tools=tools,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                if tier != start_tier:
                    log.info("Tier fallthrough: %d → %d", start_tier, tier)
                return content, tier, tokens
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if tier == 0:
                    log.warning("Tier 0 timeout/unavailable (%s), falling to tier 1", exc)
                    tier = 1
                    continue
                raise
            except Exception as exc:
                log.error("LLM call failed at tier %d: %s", tier, exc)
                raise

        raise RuntimeError("All tiers exhausted")

    async def _call(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str],
        messages: List[Dict],
        tools: Optional[List[Dict]],
        json_mode: bool,
        max_tokens: int,
        timeout: float,
    ) -> Tuple[str, int]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        content = choice["message"].get("content") or ""

        # Extract tool call content if present (Rapid-MLX / OpenRouter format)
        if not content and choice["message"].get("tool_calls"):
            tc = choice["message"]["tool_calls"][0]
            content = json.dumps({
                "tool_call": tc["function"]["name"],
                "arguments": json.loads(tc["function"].get("arguments", "{}")),
            })

        tokens = data.get("usage", {}).get("total_tokens", 0)
        return content, tokens


llm = LLMClient()

# ─── Plan Validator ────────────────────────────────────────────────────────────

class PlanValidationError(Exception):
    pass


class PlanValidator:

    def validate(self, plan: Plan) -> List[int]:
        """
        Full validation. Returns topologically sorted step IDs.
        Raises PlanValidationError on any structural issue.
        """
        self._check_step_limit(plan)
        self._check_tool_names(plan)
        sorted_ids = self._topological_sort(plan)
        self._check_decision_nesting(plan)
        self._check_branch_references(plan)
        plan.sorted_step_ids = sorted_ids
        return sorted_ids

    def _check_step_limit(self, plan: Plan) -> None:
        if len(plan.steps) > settings.max_plan_steps:
            raise PlanValidationError(
                f"Plan has {len(plan.steps)} steps; max is {settings.max_plan_steps}"
            )

    def _check_tool_names(self, plan: Plan) -> None:
        tool_names = [s.tool for s in plan.steps if s.type == StepType.TOOL and s.tool]
        invalid = harness.validate_names(tool_names)
        if invalid:
            raise PlanValidationError(f"Plan references unknown tools: {invalid}")

    def _topological_sort(self, plan: Plan) -> List[int]:
        """Kahn's algorithm. Raises on cycle."""
        all_ids = {s.id for s in plan.steps}
        in_degree: Dict[int, int] = {s.id: 0 for s in plan.steps}
        graph: Dict[int, List[int]] = defaultdict(list)

        for step in plan.steps:
            for dep in step.context_needed:
                if dep not in all_ids:
                    raise PlanValidationError(
                        f"Step {step.id} declares context_needed={dep} which doesn't exist"
                    )
                graph[dep].append(step.id)
                in_degree[step.id] += 1

        queue = [sid for sid in all_ids if in_degree[sid] == 0]
        queue.sort()
        sorted_ids: List[int] = []

        while queue:
            node = queue.pop(0)
            sorted_ids.append(node)
            for neighbor in sorted(graph[node]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(sorted_ids) != len(plan.steps):
            raise PlanValidationError("Plan contains a dependency cycle")

        return sorted_ids

    def _check_decision_nesting(self, plan: Plan) -> None:
        step_map = {s.id: s for s in plan.steps}
        for step in plan.steps:
            if step.type == StepType.DECISION and step.branches:
                for branch_steps in step.branches.values():
                    depth = self._nesting_depth(branch_steps, step_map, 1)
                    if depth > settings.max_decision_nesting_depth:
                        raise PlanValidationError(
                            f"Decision nesting depth {depth} exceeds max "
                            f"{settings.max_decision_nesting_depth} at step {step.id}"
                        )

    def _nesting_depth(self, step_ids: List[int], step_map: Dict, current: int) -> int:
        max_d = current
        for sid in step_ids:
            step = step_map.get(sid)
            if step and step.type == StepType.DECISION and step.branches:
                for branch_steps in step.branches.values():
                    d = self._nesting_depth(branch_steps, step_map, current + 1)
                    max_d = max(max_d, d)
        return max_d

    def _check_branch_references(self, plan: Plan) -> None:
        all_ids = {s.id for s in plan.steps}
        for step in plan.steps:
            if step.type == StepType.DECISION and step.branches:
                for branch_name, branch_steps in step.branches.items():
                    for sid in branch_steps:
                        if sid not in all_ids:
                            raise PlanValidationError(
                                f"Decision step {step.id} branch '{branch_name}' "
                                f"references non-existent step {sid}"
                            )


validator = PlanValidator()

# ─── Context Assembler ────────────────────────────────────────────────────────

def _truncate_middle(text: str, max_chars: int) -> str:
    """
    Middle truncation: preserve start and end, cut the middle.
    This is generally better than tail truncation for structured outputs.
    """
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n... [truncated] ...\n" + text[-half:]


def assemble_context(
    step: PlanStep,
    results: Dict[int, StepResult],
    max_chars_per_dep: int = 4000,
) -> str:
    """
    Build a focused context string from only the outputs declared in context_needed.
    Steps that were skipped produce a null-substitution notice rather than crashing.
    """
    if not step.context_needed:
        return ""

    parts: List[str] = []
    for dep_id in step.context_needed:
        result = results.get(dep_id)
        if result is None or result.status == StepStatus.SKIPPED:
            # Edge case: context_needed references a skipped branch step
            parts.append(f"[Step {dep_id} output: not available (step was skipped)]")
        elif result.status == StepStatus.EMPTY:
            parts.append(f"[Step {dep_id} output: empty — tool returned no results]")
        elif result.status == StepStatus.FAILED:
            parts.append(f"[Step {dep_id} output: FAILED — {result.error}]")
        else:
            raw = json.dumps(result.output) if not isinstance(result.output, str) else result.output
            truncated = _truncate_middle(raw, max_chars_per_dep)
            parts.append(f"[Step {dep_id} output]:\n{truncated}")

    return "\n\n".join(parts)

# ─── Planner ──────────────────────────────────────────────────────────────────

PLANNER_SYSTEM = """\
You are a task planning engine. Given a user request and a set of available tools,
produce a structured execution plan as JSON.

Rules:
- Decompose the task into sequential steps
- Each step has exactly one clear action
- Identify points where the next action depends on what a previous step returned
- At those points, insert a DECISION step with a specific BINARY condition
- Decision branches list step IDs to execute in each case
- Decision nesting must not exceed 2 levels
- context_needed lists step IDs whose output is required — keep this minimal
- If a step needs a tool, set type=tool and tool=<name>
- If a step synthesises previous outputs, set type=synthesis
- reasoning_required=true only for steps needing genuine multi-step logic

Output ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "steps": [
    {
      "id": <int>,
      "type": "tool" | "decision" | "synthesis",
      "description": "<what this step does>",
      "tool": "<tool_name or null>",
      "args_template": {<static args dict or null>},
      "context_needed": [<step ids>],
      "condition": "<binary question for decision steps, null otherwise>",
      "branches": {"yes": [<step ids>], "no": [<step ids>]} | null,
      "default_branch": "yes" | "no" | null,
      "reasoning_required": true | false,
      "forced_tier": null
    }
  ]
}
"""


class Planner:

    async def plan(self, request: str, tier: int = 0) -> Plan:
        """Generate a validated plan. Replans on validation failure (once)."""
        tool_schemas_str = json.dumps(harness.all_schemas(), indent=2)
        messages = [
            {
                "role": "system",
                # Static system prompt — safe for Rapid-MLX prompt cache
                "content": PLANNER_SYSTEM,
            },
            {
                "role": "user",
                # Dynamic content in user turn — does NOT invalidate the cache
                "content": (
                    f"Available tools:\n{tool_schemas_str}\n\n"
                    f"User request: {request}"
                ),
            },
        ]

        raw, tier_used, tokens = await llm.complete(
            messages=messages,
            tier=tier,
            json_mode=True,
            max_tokens=2048,
        )
        log.info("Plan generated at tier %d (%d tokens)", tier_used, tokens)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Planner returned invalid JSON: {exc}\nRaw: {raw[:500]}")

        plan = Plan(**data)
        validator.validate(plan)
        return plan


planner = Planner()

# ─── Executor ─────────────────────────────────────────────────────────────────

DECISION_SYSTEM = """\
You evaluate a binary condition based on provided context.
Respond ONLY with valid JSON: {"branch": "yes"|"no", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}
"""

SYNTHESIS_SYSTEM = """\
You are a synthesis engine. Use the provided context to produce a clear, concise response.
Always include a confidence score.
Respond ONLY with valid JSON: {"output": "<your response>", "confidence": <0.0-1.0>}
"""

UNEXPECTED_RESULT_SYSTEM = """\
You check whether a tool result matches its expected type.
Respond ONLY with valid JSON: {"matches_expectation": true|false, "issue": "<brief description or null>"}
"""


class Executor:

    async def execute(self, state: ExecutionState) -> ExecutionState:
        assert state.plan is not None
        plan = state.plan
        skipped: Set[int] = set()
        step_map = {s.id: s for s in plan.steps}

        state.status = TaskStatus.EXECUTING
        state.touch()

        for step_id in plan.sorted_step_ids:
            step = step_map[step_id]

            # ── Skip steps excluded by branch decisions ──────────────────
            if step_id in skipped:
                state.results[step_id] = StepResult(
                    step_id=step_id, status=StepStatus.SKIPPED
                )
                state.skipped_steps.append(step_id)
                log.info("Step %d skipped (excluded branch)", step_id)
                continue

            # ── Route tier for this step ─────────────────────────────────
            tier = self._select_tier(step)

            t0 = time.monotonic()

            if step.type == StepType.TOOL:
                result = await self._execute_tool(step, state, tier)

            elif step.type == StepType.DECISION:
                result = await self._execute_decision(step, state, tier)
                # Mark non-taken branch steps as skipped
                if result.branch_taken and step.branches:
                    for branch_name, branch_steps in step.branches.items():
                        if branch_name != result.branch_taken:
                            skipped.update(branch_steps)
                # Log every branch decision for debugging
                state.branch_log.append({
                    "step_id": step_id,
                    "condition": step.condition,
                    "branch_taken": result.branch_taken,
                    "confidence": result.confidence,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                log.info(
                    "Decision step %d: condition='%s' → branch='%s' (confidence=%.2f)",
                    step_id, step.condition, result.branch_taken, result.confidence,
                )

            elif step.type == StepType.SYNTHESIS:
                result = await self._execute_synthesis(step, state, tier)

            else:
                result = StepResult(
                    step_id=step_id,
                    status=StepStatus.FAILED,
                    error=f"Unknown step type: {step.type}",
                )

            result.execution_time_s = time.monotonic() - t0
            result.tier_used = tier

            # Flag low-confidence outputs — don't fail, but surface for inspection
            if result.confidence < settings.confidence_threshold and result.status == StepStatus.COMPLETED:
                result.flagged = True
                log.warning("Step %d flagged: confidence %.2f below threshold", step_id, result.confidence)

            state.results[step_id] = result
            state.completed_steps.append(step_id)
            state.touch()

        # ── Assemble final output from last synthesis step ───────────────
        state.final_output = self._extract_final_output(state)
        state.status = TaskStatus.COMPLETED
        state.touch()
        return state

    # ── Step executors ────────────────────────────────────────────────────────

    async def _execute_tool(
        self, step: PlanStep, state: ExecutionState, tier: int
    ) -> StepResult:
        """Execute a registered tool. Validates output shape."""
        assert step.tool is not None
        ctx = assemble_context(step, state.results)
        args = step.args_template or {}

        # If args need dynamic filling from context, ask the model to extract them
        if ctx and not args:
            args = await self._extract_args(step, ctx, tier)

        try:
            raw_output = await harness.execute(step.tool, args)
        except ToolExecutionError as exc:
            return StepResult(step_id=step.id, status=StepStatus.FAILED, error=str(exc))

        # Empty result gate
        if raw_output is None or raw_output == {} or raw_output == []:
            log.info("Step %d: tool '%s' returned empty", step.id, step.tool)
            return StepResult(
                step_id=step.id,
                status=StepStatus.EMPTY,
                output=raw_output,
                confidence=1.0,
            )

        # Unexpected result gate — lightweight check before continuing
        ok, issue = await self._check_output_shape(step, raw_output, tier)
        if not ok:
            log.warning("Step %d unexpected result shape: %s", step.id, issue)
            # Don't fail — record the issue and continue; branch conditions handle it
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                output=raw_output,
                confidence=0.5,
                flagged=True,
                error=f"Unexpected shape: {issue}",
            )

        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            output=raw_output,
            confidence=1.0,
        )

    async def _execute_decision(
        self, step: PlanStep, state: ExecutionState, tier: int
    ) -> StepResult:
        """Evaluate a binary branch condition. Never fails — uses default_branch."""
        ctx = assemble_context(step, state.results)
        messages = [
            {"role": "system", "content": DECISION_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Condition to evaluate: {step.condition}\n\n"
                    f"Available context:\n{ctx}"
                ),
            },
        ]

        try:
            raw, _, tokens = await llm.complete(
                messages=messages,
                tier=tier,
                json_mode=True,
                max_tokens=256,
            )
            data = json.loads(raw)
            branch = data.get("branch", step.default_branch or "yes")
            confidence = float(data.get("confidence", 0.5))

            # Validate branch name exists in the plan
            if step.branches and branch not in step.branches:
                log.warning(
                    "Decision step %d returned unknown branch '%s', using default '%s'",
                    step.id, branch, step.default_branch,
                )
                branch = step.default_branch or next(iter(step.branches))

        except Exception as exc:
            log.warning("Decision step %d failed (%s), using default branch", step.id, exc)
            branch = step.default_branch or (
                next(iter(step.branches)) if step.branches else "yes"
            )
            confidence = 0.0
            tokens = 0

        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            branch_taken=branch,
            confidence=confidence,
            tokens_used=tokens,
        )

    async def _execute_synthesis(
        self, step: PlanStep, state: ExecutionState, tier: int
    ) -> StepResult:
        """Synthesise prior step outputs into a coherent response."""
        ctx = assemble_context(step, state.results)
        messages = [
            {"role": "system", "content": SYNTHESIS_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Task: {step.description}\n\n"
                    f"Context from prior steps:\n{ctx}"
                ),
            },
        ]

        try:
            raw, _, tokens = await llm.complete(
                messages=messages,
                tier=tier,
                json_mode=True,
                max_tokens=settings.max_step_output_tokens,
            )
            data = json.loads(raw)
            output = data.get("output", raw)
            confidence = float(data.get("confidence", 0.9))
        except Exception as exc:
            return StepResult(
                step_id=step.id,
                status=StepStatus.FAILED,
                error=str(exc),
            )

        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            output=output,
            confidence=confidence,
            tokens_used=tokens,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _select_tier(self, step: PlanStep) -> int:
        """Deterministic tier selection — model never decides this."""
        if step.forced_tier is not None:
            return step.forced_tier
        if step.type == StepType.DECISION:
            return 0   # Decision steps are cheap; always try local first
        if step.type == StepType.TOOL:
            return 0   # Tool steps: simple, local first
        if step.reasoning_required:
            return 2   # Hard synthesis → tier 2
        return 0       # Default: try local

    async def _extract_args(self, step: PlanStep, ctx: str, tier: int) -> Dict:
        """Ask model to extract tool arguments from context."""
        schema = harness.schema_for(step.tool or "")
        prompt = (
            f"Extract the arguments for tool '{step.tool}' from the context below.\n"
            f"Tool schema: {json.dumps(schema)}\n\n"
            f"Context:\n{ctx}\n\n"
            "Respond ONLY with a JSON object of the arguments."
        )
        try:
            raw, _, _ = await llm.complete(
                messages=[{"role": "user", "content": prompt}],
                tier=tier,
                json_mode=True,
                max_tokens=512,
            )
            return json.loads(raw)
        except Exception:
            return {}

    async def _check_output_shape(
        self, step: PlanStep, output: Any, tier: int
    ) -> Tuple[bool, Optional[str]]:
        """Lightweight unexpected-result gate."""
        output_str = json.dumps(output)[:1000]
        prompt = (
            f"Tool: {step.tool}\nDescription: {step.description}\n"
            f"Output received:\n{output_str}"
        )
        try:
            raw, _, _ = await llm.complete(
                messages=[
                    {"role": "system", "content": UNEXPECTED_RESULT_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                tier=0,  # Always local — this is a cheap check
                json_mode=True,
                max_tokens=128,
            )
            data = json.loads(raw)
            ok = bool(data.get("matches_expectation", True))
            issue = data.get("issue")
            return ok, issue
        except Exception:
            return True, None  # If the check itself fails, don't block execution

    def _extract_final_output(self, state: ExecutionState) -> Optional[str]:
        """Return the last successful synthesis step's output as the final answer."""
        plan = state.plan
        assert plan is not None
        for step_id in reversed(plan.sorted_step_ids):
            step = next((s for s in plan.steps if s.id == step_id), None)
            if step and step.type == StepType.SYNTHESIS:
                result = state.results.get(step_id)
                if result and result.status == StepStatus.COMPLETED:
                    return str(result.output)
        return None


executor = Executor()

# ─── Orchestrator ─────────────────────────────────────────────────────────────

class Orchestrator:

    async def run(self, task_id: str) -> None:
        state = _tasks.get(task_id)
        if not state:
            return

        try:
            # ── 1. Plan ──────────────────────────────────────────────────
            state.status = TaskStatus.PLANNING
            state.touch()
            log.info("[%s] Planning...", task_id)

            replan_count = 0
            plan = None
            last_error: Optional[str] = None

            while replan_count <= settings.max_replan_attempts:
                try:
                    plan = await planner.plan(state.user_request)
                    break
                except (PlanValidationError, ValueError) as exc:
                    last_error = str(exc)
                    log.warning("[%s] Plan invalid (attempt %d): %s", task_id, replan_count, exc)
                    replan_count += 1

            if plan is None:
                state.status = TaskStatus.FAILED
                state.error = f"Planning failed after {replan_count} attempts: {last_error}"
                state.touch()
                return

            state.plan = plan
            state.replan_count = replan_count
            log.info("[%s] Plan validated: %d steps", task_id, len(plan.steps))

            # ── 2. Optional confirmation gate ────────────────────────────
            if settings.confirm_plans:
                summary = "\n".join(
                    f"  {s.id}. [{s.type}] {s.description}" for s in plan.steps
                )
                log.info("[%s] Plan summary (awaiting confirmation):\n%s", task_id, summary)
                state.status = TaskStatus.AWAITING_CONFIRMATION
                state.touch()
                return   # Caller resumes via POST /task/{id}/resume

            # ── 3. Execute ───────────────────────────────────────────────
            await executor.execute(state)
            log.info("[%s] Completed.", task_id)

        except Exception as exc:
            log.exception("[%s] Unhandled error: %s", task_id, exc)
            state.status = TaskStatus.FAILED
            state.error = str(exc)
            state.touch()


orchestrator = Orchestrator()

# ─── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(title="Tiered Planning Agent", version="1.0.0")


class TaskRequest(BaseModel):
    request: str = Field(..., description="Natural language task description")
    tier_override: Optional[int] = Field(None, ge=0, le=3)


@app.post("/task", status_code=202)
async def submit_task(body: TaskRequest, background_tasks: BackgroundTasks) -> Dict:
    task_id = str(uuid.uuid4())
    state = ExecutionState(task_id=task_id, user_request=body.request)
    _tasks[task_id] = state
    background_tasks.add_task(orchestrator.run, task_id)
    return {"task_id": task_id, "status": state.status}


@app.get("/task/{task_id}")
async def get_task(task_id: str) -> Dict:
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="Task not found")
    return state.model_dump()


@app.post("/task/{task_id}/resume")
async def resume_task(task_id: str, background_tasks: BackgroundTasks) -> Dict:
    """Resume a task paused at AWAITING_CONFIRMATION or continue after PAUSED."""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="Task not found")
    if state.status not in (TaskStatus.AWAITING_CONFIRMATION, TaskStatus.PAUSED):
        raise HTTPException(
            status_code=400,
            detail=f"Task is in status '{state.status}', not resumable",
        )
    background_tasks.add_task(executor.execute, state)
    return {"task_id": task_id, "status": "resuming"}


@app.get("/task/{task_id}/branch-log")
async def get_branch_log(task_id: str) -> Dict:
    """Return the full branch decision log for debugging silent wrong-branch cascades."""
    state = _tasks.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task_id,
        "branch_log": state.branch_log,
        "flagged_steps": [
            {"step_id": r.step_id, "confidence": r.confidence, "error": r.error}
            for r in state.results.values()
            if r.flagged
        ],
    }


@app.get("/tools")
async def list_tools() -> Dict:
    return {"tools": harness.all_schemas()}


@app.get("/health")
async def health() -> Dict:
    return {"status": "ok", "tasks_in_memory": len(_tasks)}


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    # Pick up API key from environment if not already set
    if not settings.openrouter_api_key:
        settings.openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")

    uvicorn.run(
        "agent_server:app",
        host="0.0.0.0",
        port=8888,
        reload=False,
        log_level="info",
    )
