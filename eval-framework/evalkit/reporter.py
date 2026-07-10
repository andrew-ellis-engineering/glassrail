"""Reporting: serialize trials/scores to disk and pretty-print results.

Artifacts (principle 7 — trials are the unit of truth, re-gradable later):

    results/<run>/run_metadata.json
    results/<run>/<task-id>/task_metadata.json
    results/<run>/<task-id>/trial-NN/{trial,score,stdout}.json
    results/<run>/<task-id>/trial-NN/stderr.txt
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from evalkit import config, stats
from evalkit.models import Score, SuiteResult, TaskResult, Trial


def infra_error_stats(task_results: list[TaskResult]) -> tuple[int, int, float]:
    """(infra-failed trials, total trials, rate) across a run's scores.

    A trial counts as infra-failed when its Score.infra_error is set — a subject
    crash / empty reply / provider error, or an unreachable LLM judge. The rate
    drives the likely-invalid tripwire (``config.INVALID_RUN_INFRA_RATE``).
    """
    total = sum(len(tr.scores) for tr in task_results)
    infra = sum(1 for tr in task_results for s in tr.scores if s.infra_error)
    return infra, total, (infra / total if total else 0.0)


def run_is_invalid(task_results: list[TaskResult]) -> bool:
    """Whether infrastructure failures reach the run-invalidity threshold."""
    _infra, total, rate = infra_error_stats(task_results)
    return total > 0 and rate >= config.INVALID_RUN_INFRA_RATE


def _quality_scores(result: TaskResult) -> list[Score]:
    return [score for score in result.scores if score.graded and not score.infra_error]


def _task_score_counts(result: TaskResult) -> dict[str, int | bool]:
    graded = sum(1 for score in result.scores if score.graded)
    infra = sum(1 for score in result.scores if score.infra_error)
    quality = len(_quality_scores(result))
    return {
        "attempted_trials": len(result.trials),
        "graded_trials": graded,
        "model_quality_trials": quality,
        "excluded_infra_trials": infra,
        "metrics_valid": quality > 0,
    }


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(obj), indent=2), encoding="utf-8")


def _task_metadata(result: TaskResult) -> dict[str, Any]:
    return {
        "id": result.task.id,
        "name": result.task.name,
        "suite": result.task.suite,
        "path": str(result.task.path),
        "type": result.task.type,
        "difficulty": result.task.difficulty,
        "backend": result.task.backend,
        "model": result.task.model,
        "control_for": result.task.control_for,
        "tags": result.task.tags,
        "num_criteria": len(result.task.criteria),
        "pass_at_k": result.pass_at_k,
        "pass_pow_k": result.pass_pow_k,
        "mean_pass_rate": result.mean_pass_rate,
        "mean_tokens": _task_tokens(result)[1],
        **_task_score_counts(result),
    }


# ── Saving ─────────────────────────────────────────────────────────────────


def save_task_artifacts(run_dir: Path, result: TaskResult) -> None:
    task_dir = run_dir / result.task.id
    _write_json(task_dir / "task_metadata.json", _task_metadata(result))
    scores_by_run = {score.trial_num: score for score in result.scores}
    for trial in result.trials:
        td = task_dir / f"trial-{trial.run_number:02d}"
        _write_json(td / "trial.json", trial)
        score = scores_by_run.get(trial.run_number)
        if score is not None:
            _write_json(td / "score.json", score)
        _write_json(td / "stdout.json", trial.output_envelope)
        (td / "stderr.txt").write_text(trial.raw_stderr, encoding="utf-8")


def save_run_metadata(run_dir: Path, suite: SuiteResult) -> None:
    infra_trials, total, infra_rate = infra_error_stats(suite.task_results)
    quality_trials = sum(len(_quality_scores(result)) for result in suite.task_results)
    graded_trials = sum(
        1 for result in suite.task_results for score in result.scores if score.graded
    )
    _write_json(
        run_dir / "run_metadata.json",
        {
            "suite_name": suite.suite_name,
            "run_name": suite.run_name,
            "started_at": suite.started_at,
            "completed_at": suite.completed_at,
            "model": suite.model,
            "grader_model": suite.grader_model,
            "harness_version": suite.harness_version,
            "trials_per_task": suite.trials_per_task,
            "total_cost_usd": suite.total_cost_usd,
            "total_tokens": suite.total_tokens,
            "infra_error_trials": infra_trials,
            "infra_error_rate": round(infra_rate, 4),
            "flagged_invalid": total > 0 and infra_rate >= config.INVALID_RUN_INFRA_RATE,
            "graded_trials": graded_trials,
            "model_quality_trials": quality_trials,
            "agent_seconds_total": sum(_task_seconds(tr)[0] for tr in suite.task_results),
            "wall_seconds": (
                (suite.completed_at - suite.started_at).total_seconds()
                if suite.completed_at
                else None
            ),
            "tasks": [
                {
                    "id": tr.task.id,
                    "mean_trial_seconds": _task_seconds(tr)[1],
                    "mean_tokens": _task_tokens(tr)[1],
                }
                for tr in suite.task_results
            ],
        },
    )


# ── Loading archived trials (for re-grading) ────────────────────────────────


def load_trial(path: Path) -> Trial:
    d = json.loads(path.read_text(encoding="utf-8"))
    completed = d.get("completed_at")
    return Trial(
        task_id=d["task_id"],
        run_number=int(d["run_number"]),
        started_at=datetime.fromisoformat(d["started_at"]),
        completed_at=datetime.fromisoformat(completed) if completed else None,
        success=bool(d.get("success", False)),
        error=d.get("error"),
        duration_s=float(d.get("duration_s", 0.0)),
        output_envelope=d.get("output_envelope", {}),
        result_text=d.get("result_text", ""),
        raw_stdout=d.get("raw_stdout", ""),
        raw_stderr=d.get("raw_stderr", ""),
        trajectory=d.get("trajectory", []),
        side_effects=d.get("side_effects", {}),
        cost_usd=d.get("cost_usd"),
        total_tokens=d.get("total_tokens"),
        model=d.get("model", ""),
        harness_version=d.get("harness_version", ""),
        baseline=d.get("baseline", {}),
        infra_error=bool(d.get("infra_error", False)),
    )


def save_task_scores(run_dir: Path, result: TaskResult) -> None:
    """Persist re-graded scores without touching trial/stdout/stderr artifacts."""
    task_dir = run_dir / result.task.id
    _write_json(task_dir / "task_metadata.json", _task_metadata(result))
    for score in result.scores:
        td = task_dir / f"trial-{score.trial_num:02d}"
        _write_json(td / "score.json", score)


def update_run_metadata_scores(run_dir: Path, task_results: list[TaskResult]) -> None:
    """Refresh run-level fields after a trustworthy suite re-grade."""
    path = run_dir / "run_metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    infra, total, rate = infra_error_stats(task_results)
    metadata.update(
        {
            "infra_error_trials": infra,
            "infra_error_rate": round(rate, 4),
            "flagged_invalid": total > 0 and rate >= config.INVALID_RUN_INFRA_RATE,
            "graded_trials": sum(
                1 for result in task_results for score in result.scores if score.graded
            ),
            "model_quality_trials": sum(
                len(_quality_scores(result)) for result in task_results
            ),
            "scoring_harness_version": config.HARNESS_VERSION,
            "regraded_at": datetime.now().astimezone(),
        }
    )
    _write_json(path, metadata)


def load_archived_trials(task_results_dir: Path) -> list[Trial]:
    trials: list[Trial] = []
    for trial_dir in sorted(task_results_dir.glob("trial-*")):
        trial_json = trial_dir / "trial.json"
        if trial_json.exists():
            trials.append(load_trial(trial_json))
    return trials


# ── Pretty-printing ──────────────────────────────────────────────────────────


def _cell(passed: bool) -> str:
    return "PASS" if passed else "FAIL"


def _task_seconds(result: TaskResult) -> tuple[float, float]:
    """Return (total, mean) subject wall-time in seconds across a task's trials."""
    durs = [t.duration_s for t in result.trials]
    total = sum(durs)
    return total, (total / len(durs) if durs else 0.0)


def _fmt_secs(s: float) -> str:
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f}m"


def _task_tokens(result: TaskResult) -> tuple[int | None, float | None]:
    vals = [t.total_tokens for t in result.trials if t.total_tokens is not None]
    if not vals:
        return None, None
    total = sum(vals)
    return total, total / len(vals)


def _fmt_tokens(tokens: float | None) -> str:
    return "-" if tokens is None else f"{tokens:.0f}"


def print_task_result(result: TaskResult) -> None:
    scores = result.scores
    quality_scores = _quality_scores(result)
    k = len(quality_scores)
    print(f"\n── {result.task.id}  [{result.task.type}, difficulty {result.task.difficulty}] ──")
    if not scores:
        print("  (no trials)")
        return

    if not any(score.graded for score in scores):
        infra_count = sum(1 for score in scores if score.infra_error)
        print(f"  grading skipped; infrastructure failures: {infra_count}/{len(scores)}")
        return

    crit_texts = [c.criterion_text for c in scores[0].criterion_results]
    grader_for = {c.criterion_text: c.grader_used for c in scores[0].criterion_results}
    width = min(60, max((len(t) for t in crit_texts), default=10))

    header = (
        f"  {'criterion':<{width}} "
        + " ".join(f"T{i + 1:<4}" for i in range(len(scores)))
        + " grader"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for ci, text in enumerate(crit_texts):
        cells = " ".join(f"{_cell(s.criterion_results[ci].passed):<5}" for s in scores)
        label = text if len(text) <= width else text[: width - 1] + "…"
        print(f"  {label:<{width}} {cells} {grader_for[text]}")

    perfect = sum(1 for s in quality_scores if s.pass_rate == 1.0)
    infra_count = sum(1 for s in scores if s.infra_error)
    lo, hi = stats.wilson_ci(perfect, k)
    _total, mean_s = _task_seconds(result)
    _tokens_total, mean_tokens = _task_tokens(result)
    infra_note = f"  ⚠ {infra_count}/{len(scores)} infra-error" if infra_count else ""
    if result.pass_at_k is None or result.pass_pow_k is None or result.mean_pass_rate is None:
        metrics = "model-quality metrics unavailable (no trustworthy graded trials)"
    else:
        metrics = (
            f"pass@{k}={result.pass_at_k:.2f}  pass^{k}={result.pass_pow_k:.2f}  "
            f"mean={result.mean_pass_rate:.2f}  pass^k 95% CI=[{lo:.2f}, {hi:.2f}]"
        )
    print(
        f"  → {metrics}  mean trial={_fmt_secs(mean_s)}  "
        f"mean tokens={_fmt_tokens(mean_tokens)}{infra_note}"
    )


def print_suite_summary(suite: SuiteResult) -> None:
    print(f"\n{'=' * 64}")
    print(f"  SUITE {suite.suite_name}  ({suite.run_name})")
    print(f"  model={suite.model}  grader={suite.grader_model}  harness={suite.harness_version}")
    print(f"{'=' * 64}")
    reg_fail = 0
    total_infra = 0
    agent_secs = 0.0
    for tr in suite.task_results:
        flag = ""
        infra_count = sum(1 for s in tr.scores if s.infra_error)
        total_infra += infra_count
        if tr.task.type == "regression" and tr.pass_pow_k == 0.0:
            flag = "  ← REGRESSION (pass^k=0)"
            reg_fail += 1
        if infra_count:
            flag += f"  ⚠{infra_count}infra"
        total_s, mean_s = _task_seconds(tr)
        _tokens_total, mean_tokens = _task_tokens(tr)
        agent_secs += total_s
        pass_at = "N/A" if tr.pass_at_k is None else f"{tr.pass_at_k:.2f}"
        pass_pow = "N/A" if tr.pass_pow_k is None else f"{tr.pass_pow_k:.2f}"
        print(
            f"  {tr.task.id:<28} {tr.task.type:<11} "
            f"pass@k={pass_at:>4} pass^k={pass_pow:>4} "
            f"{_fmt_secs(mean_s):>6}/trial tokens={_fmt_tokens(mean_tokens):>6}{flag}"
        )
    _print_control_concordance(suite)
    print(f"  {'-' * 60}")
    wall = (
        (suite.completed_at - suite.started_at).total_seconds()
        if suite.completed_at
        else agent_secs
    )
    print(
        f"  agent time: {_fmt_secs(agent_secs)} over {suite.trials_per_task * len(suite.task_results)}"
        f" trials   wall clock: {_fmt_secs(wall)}"
    )
    infra_note = f"   infra-errors: {total_infra}" if total_infra else ""
    print(f"  regression failures: {reg_fail}   total cost: ${suite.total_cost_usd:.4f}{infra_note}")
    _infra, infra_total, infra_rate = infra_error_stats(suite.task_results)
    if infra_total > 0 and infra_rate >= config.INVALID_RUN_INFRA_RATE:
        judge_hit = sum(
            1
            for tr in suite.task_results
            for s in tr.scores
            if s.infra_error
            and any("judge invocation failed" in (c.evidence or "") for c in s.criterion_results)
        )
        print("  " + "!" * 60)
        print(
            f"  ⚠ RUN INVALID — {total_infra}/{infra_total} trials "
            f"({infra_rate:.0%}) hit infra failures (≥{config.INVALID_RUN_INFRA_RATE:.0%} tripwire)."
        )
        print(
            f"    subject crash/empty/provider: {total_infra - judge_hit}   "
            f"unreachable judge: {judge_hit}.  Broken plumbing, not the model — "
            "fix and re-run; do not publish these numbers."
        )
        print("  " + "!" * 60)
    print(f"{'=' * 64}\n")


def _print_control_concordance(suite: SuiteResult) -> None:
    by_id = {tr.task.id: tr for tr in suite.task_results}
    for tr in suite.task_results:
        ctrl = tr.task.control_for
        if ctrl and ctrl in by_id:
            a_ok = tr.pass_pow_k == 1.0
            b_ok = by_id[ctrl].pass_pow_k == 1.0
            verdict = "concordant ✓" if (a_ok and b_ok) else "SUSPICIOUS (one side fails)"
            print(f"  control pair: {tr.task.id} ↔ {ctrl}: {verdict}")
