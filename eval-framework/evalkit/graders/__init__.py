"""Grader dispatcher — the hybrid cascade (principle 5).

Grade in order: deterministic → trajectory → LLM. Deterministic and trajectory
checks are cheap and decisive; LLM judges fire last and, by default, are
skipped entirely when every deterministic criterion already failed (cost gate).
The returned :class:`Score` preserves the criteria's original order.
"""

from __future__ import annotations

from evalkit.graders import deterministic, llm, trajectory
from evalkit.judge import Judge
from evalkit.models import CriterionResult, Score, Task, Trial


def grade(task: Task, trial: Trial, *, judge: Judge, cost_optimize: bool = True) -> Score:
    results: dict[int, CriterionResult] = {}

    det = [i for i, c in enumerate(task.criteria) if c.grader == "deterministic"]
    traj = [i for i, c in enumerate(task.criteria) if c.grader == "trajectory"]
    judged = [i for i, c in enumerate(task.criteria) if c.grader == "llm"]

    for i in det:
        results[i] = deterministic.grade(task.criteria[i], trial)
    for i in traj:
        results[i] = trajectory.grade(task.criteria[i], trial)

    # Cost gate: if there are deterministic criteria and none passed, the trial
    # is already a fail — don't pay for LLM judging.
    any_det_passed = any(results[i].passed for i in det)
    skip_llm = cost_optimize and bool(det) and not any_det_passed
    for i in judged:
        if skip_llm:
            results[i] = CriterionResult(
                criterion_text=task.criteria[i].text,
                passed=False,
                evidence="skipped: all deterministic criteria failed (cost gate)",
                grader_used="llm",
            )
        else:
            results[i] = llm.grade(
                task.criteria[i],
                trial,
                expected_behavior=task.expected_behavior,
                judge=judge,
            )

    ordered = [results[i] for i in range(len(task.criteria))]
    passed = sum(1 for r in ordered if r.passed)
    total = len(ordered)

    # Detect infrastructure failures so they are not silently counted as model
    # quality failures.  Signals: planning/provider error strings, OR a trial
    # that produced neither result text nor trajectory with no recorded error
    # (the "empty trajectory, no error" fingerprint of a silent tier-0 failure).
    _infra_keywords = ("timed out", "planning failed", "provider", "http ", "connect")
    error_str = (trial.error or "").lower()
    infra_error = bool(trial.error and any(kw in error_str for kw in _infra_keywords)) or (
        not trial.success
        and not trial.result_text
        and not trial.trajectory
        and trial.error is None
    )

    return Score(
        task_id=task.id,
        trial_num=trial.run_number,
        criterion_results=ordered,
        passed=passed,
        failed=total - passed,
        total=total,
        pass_rate=(passed / total) if total else 0.0,
        infra_error=infra_error,
    )
