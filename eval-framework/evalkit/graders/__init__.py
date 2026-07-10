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


_INFRA_ERROR_KEYWORDS = (
    "timed out",
    "cli not found",
    "could not parse",
    "endpoint error",
    "gateway unreachable",
    "gateway poll failed",
    "http ",
    "connect",
    "provider unavailable",
    "payment required",
    "credit",
    "quota",
    "rate limit",
    "missing openrouter credentials",
    "empty completion",
)
_ENVELOPE_INFRA_KEYWORDS = (
    "provider unavailable",
    "payment required",
    "insufficient credit",
    "quota exceeded",
    "rate limit",
    "connection refused",
    "connection reset",
    "timed out",
)


def trial_infra_error(trial: Trial) -> bool:
    """Classify subject/runtime failures, including pre-v0.5 archived trials.

    New subjects stamp ``Trial.infra_error`` explicitly. The fallback keeps old
    archives re-gradable: transport-like errors and failures with no parseable
    envelope or gradeable evidence are infrastructure failures. A parseable
    model failure envelope remains a model-quality outcome.
    """
    if trial.infra_error:
        return True
    if trial.success:
        return False
    error = (trial.error or "").lower()
    if any(keyword in error for keyword in _INFRA_ERROR_KEYWORDS):
        return True
    envelope_text = str(trial.output_envelope).lower()
    if any(keyword in envelope_text for keyword in _ENVELOPE_INFRA_KEYWORDS):
        return True
    no_evidence = not trial.result_text and not trial.trajectory
    return no_evidence and not trial.output_envelope


def ungraded_score(task: Task, trial: Trial) -> Score:
    """Record infrastructure status for --skip-grading without quality scores."""
    return Score(
        task_id=task.id,
        trial_num=trial.run_number,
        criterion_results=[],
        passed=0,
        failed=0,
        total=0,
        pass_rate=0.0,
        infra_error=trial_infra_error(trial),
        graded=False,
    )


def _subject_infra_score(task: Task, trial: Trial) -> Score:
    results = [
        CriterionResult(
            criterion_text=criterion.text,
            passed=False,
            evidence="not graded: subject infrastructure failure",
            grader_used=criterion.grader,
            infra_error=True,
        )
        for criterion in task.criteria
    ]
    return Score(
        task_id=task.id,
        trial_num=trial.run_number,
        criterion_results=results,
        passed=0,
        failed=len(results),
        total=len(results),
        pass_rate=0.0,
        infra_error=True,
    )


def grade(task: Task, trial: Trial, *, judge: Judge, cost_optimize: bool = True) -> Score:
    if trial_infra_error(trial):
        return _subject_infra_score(task, trial)

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

    infra_error = any(result.infra_error for result in ordered)

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
