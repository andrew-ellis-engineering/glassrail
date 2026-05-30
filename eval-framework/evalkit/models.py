"""Dataclasses for the eval framework. No Pydantic — stdlib only.

The :class:`Trial` record is the unit of truth (principle 7): it carries
enough captured evidence — result text, side-effects, full trajectory, and a
pre-run ``baseline`` snapshot — to re-grade with new criteria at zero
inference cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class FixtureSpec:
    install: dict[str, str | None]  # {dest_path: source_name | None (= delete)}
    capture: list[str]              # paths to read post-execution


@dataclass
class Criterion:
    text: str                       # human-readable description
    grader: str                     # "deterministic" | "trajectory" | "llm"
    check: str | None = None        # deterministic: file_exists, file_absent,
                                    # contains, not_contains, regex, not_regex,
                                    # json_field, line_count, file_unchanged
    target: str | None = None       # path or "__result_text__"
    value: Any = None               # check-specific
    tool_sequence: list[str] | None = None  # for trajectory


@dataclass
class Task:
    id: str                         # = task directory name
    name: str
    suite: str
    path: Path
    prompt: str                     # contents of prompt.md
    model: str                      # "sonnet" | "opus" | "haiku"
    max_turns: int
    timeout_s: int
    tags: list[str]
    type: str                       # "regression" | "capability"
    difficulty: int                 # 1-5
    control_for: str | None         # paired task ID
    expected_behavior: str          # for LLM judge context, NOT shown to agent
    criteria: list[Criterion]
    fixtures: FixtureSpec
    context_files: dict[str, str]   # name → content
    # Which subject (system under test) runs this task, and its wiring config.
    # Defaults keep older claude-skill suites working unchanged.
    backend: str = "claude-cli"     # claude-cli | dagagent-cli | dagagent-gateway | openai-compat
    backend_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trial:
    task_id: str
    run_number: int
    started_at: datetime
    completed_at: datetime | None
    success: bool
    error: str | None
    duration_s: float
    output_envelope: dict[str, Any]      # raw subject envelope (backend-specific)
    result_text: str                     # primary output text to grade
    raw_stdout: str
    raw_stderr: str
    trajectory: list[dict[str, Any]]     # normalized steps [{tool: str, input: dict, ...}, ...]
    side_effects: dict[str, str | None]  # captured paths → content (post-run)
    cost_usd: float | None
    model: str
    harness_version: str
    # Pre-run content of capture paths, so file_unchanged can compare without
    # re-reading live state (keeps grading decoupled from the environment).
    baseline: dict[str, str | None] = field(default_factory=dict)


@dataclass
class CriterionResult:
    criterion_text: str
    passed: bool
    evidence: str                   # human-readable explanation
    grader_used: str


@dataclass
class Score:
    task_id: str
    trial_num: int
    criterion_results: list[CriterionResult]
    passed: int
    failed: int
    total: int
    pass_rate: float                # passed / total


@dataclass
class TaskResult:
    task: Task
    trials: list[Trial]
    scores: list[Score]
    pass_at_k: float
    pass_pow_k: float
    mean_pass_rate: float


@dataclass
class SuiteResult:
    suite_name: str
    run_name: str
    started_at: datetime
    completed_at: datetime | None
    model: str
    grader_model: str
    harness_version: str
    trials_per_task: int
    task_results: list[TaskResult]
    total_cost_usd: float
