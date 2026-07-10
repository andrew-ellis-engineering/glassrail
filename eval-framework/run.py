#!/usr/bin/env python3
"""Eval framework CLI.

Subcommands: task, suite, list, score, score-suite, promote, demote, candidates.

Exit codes: 0 = success, 1 = a regression task had pass^k = 0 (CI gating
signal), 2 = framework error.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from evalkit import config, graders, loader, ratchet, reporter, runner, stats, subjects
from evalkit.judge import Judge, build_judge
from evalkit.loader import LoaderError
from evalkit.models import Score, SuiteResult, Task, TaskResult, Trial

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2


# ── Orchestration ────────────────────────────────────────────────────────────


def _run_name(arg: str | None) -> str:
    return arg or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")


def build_task_result(task: Task, trials: list[Trial], scores: list[Score]) -> TaskResult:
    quality_scores = [score for score in scores if score.graded and not score.infra_error]
    n = len(quality_scores)
    perfect = sum(1 for score in quality_scores if score.pass_rate == 1.0)
    return TaskResult(
        task=task,
        trials=trials,
        scores=scores,
        pass_at_k=stats.pass_at_k(n, perfect, n) if n else None,
        pass_pow_k=stats.pass_pow_k(quality_scores) if n else None,
        mean_pass_rate=stats.mean_pass_rate(quality_scores) if n else None,
    )


def _results_exit_code(results: list[TaskResult]) -> int:
    """Return infrastructure invalidity before considering regression gates."""
    _infra, _total, infra_rate = reporter.infra_error_stats(results)
    if infra_rate >= config.INVALID_RUN_INFRA_RATE:
        return EXIT_ERROR
    regression_failed = any(
        result.task.type == "regression" and result.pass_pow_k == 0.0 for result in results
    )
    return EXIT_REGRESSION if regression_failed else EXIT_OK


def _make_judge(meta: dict[str, Any], args: argparse.Namespace) -> Judge:
    """Build the judge callable from CLI flags falling back to suite defaults."""
    grader_model = (
        args.grader_model or meta.get("default_grader_model") or config.DEFAULT_GRADER_MODEL
    )
    grader_backend = (
        getattr(args, "grader_backend", None)
        or meta.get("default_grader_backend")
        or config.DEFAULT_JUDGE_BACKEND
    )
    return build_judge(
        model=str(grader_model),
        backend=str(grader_backend),
        config=meta.get("judge_config") or {},
    )


def _tier_model_overrides(args: argparse.Namespace) -> dict[int, str]:
    """Parse tier-specific model overrides from CLI args.

    Supported forms:
      --tier-model 1=openai/gpt-5.4-mini
      --tier1-model openai/gpt-5.4-mini
    """
    overrides: dict[int, str] = {}
    for raw in getattr(args, "tier_model", None) or []:
        if "=" not in raw:
            raise LoaderError(f"--tier-model expects TIER=MODEL, got {raw!r}")
        tier_raw, model = raw.split("=", 1)
        try:
            tier = int(tier_raw)
        except ValueError as exc:
            raise LoaderError(
                f"--tier-model tier must be an integer 0-3, got {tier_raw!r}"
            ) from exc
        if tier < 0 or tier > 3:
            raise LoaderError(f"--tier-model tier must be 0-3, got {tier}")
        if not model.strip():
            raise LoaderError(f"--tier-model {tier}=... needs a non-empty model")
        overrides[tier] = model.strip()

    for tier in range(4):
        model = getattr(args, f"tier{tier}_model", None)
        if model:
            overrides[tier] = str(model)
    return overrides


def _backend_config_with_tier_models(
    backend_config: dict[str, Any], tier_models: dict[int, str]
) -> dict[str, Any]:
    """Return backend config with Glassrail tier model env overrides applied."""
    if not tier_models:
        return backend_config
    updated = dict(backend_config)
    env = dict(updated.get("env") or {})
    for tier, model in sorted(tier_models.items()):
        env[f"GLASSRAIL_TIER{tier}__MODEL"] = model
    updated["env"] = env
    return updated


def _format_tier_models(tier_models: dict[int, str]) -> str:
    return ", ".join(f"tier{tier}={model}" for tier, model in sorted(tier_models.items()))


def _model_label(default_model: str, tier_models: dict[int, str]) -> str:
    if not tier_models:
        return default_model
    return f"{default_model} ({_format_tier_models(tier_models)})"


# ── Parallel-execution helpers ────────────────────────────────────────────────

# Per-fixture-path locks: tasks that manage the same /tmp path serialize against
# each other; tasks with disjoint fixture sets run fully in parallel.
_fixture_path_locks: dict[str, threading.Lock] = {}
_fixture_path_locks_guard = threading.Lock()
# Serialise progress prints so interleaved workers don't garble output.
_print_lock = threading.Lock()


def _get_fixture_lock(resolved_path: str) -> threading.Lock:
    with _fixture_path_locks_guard:
        if resolved_path not in _fixture_path_locks:
            _fixture_path_locks[resolved_path] = threading.Lock()
        return _fixture_path_locks[resolved_path]


def _task_managed_paths(task: Task) -> list[str]:
    """Return sorted resolved fixture paths this task manages.

    Sorting is required to acquire multiple locks in a deterministic order and
    avoid deadlock when two tasks share a subset of fixture paths.
    """
    seen: set[str] = set()
    paths: list[str] = []
    for raw in [*task.fixtures.install.keys(), *task.fixtures.capture]:
        resolved = str(Path(raw).expanduser().resolve())
        if resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    return sorted(paths)


def _run_task_locked(
    task: Task,
    *,
    trials: int,
    model: str | None,
    judge: Judge,
    timeout: int | None,
    skip_grading: bool,
    backend_override: str | None,
    tier_models: dict[int, str] | None,
) -> TaskResult:
    """Run *task* while holding its fixture-path locks for the full duration.

    Acquiring all locks before any trial starts (and releasing after the last)
    ensures that two workers cannot concurrently back up / install / restore
    the same filesystem path.  Tasks with disjoint fixture sets acquire
    disjoint lock sets and therefore run fully in parallel.
    """
    managed = _task_managed_paths(task)
    locks = [_get_fixture_lock(p) for p in managed]
    for lock in locks:
        lock.acquire()
    try:
        return run_task(
            task,
            trials=trials,
            model=model,
            judge=judge,
            timeout=timeout,
            skip_grading=skip_grading,
            backend_override=backend_override,
            tier_models=tier_models,
        )
    finally:
        for lock in locks:
            lock.release()


def run_task(
    task: Task,
    *,
    trials: int,
    model: str | None,
    judge: Judge,
    timeout: int | None,
    skip_grading: bool,
    backend_override: str | None = None,
    tier_models: dict[int, str] | None = None,
) -> TaskResult:
    backend = backend_override or task.backend
    subject = subjects.build_subject(
        backend, _backend_config_with_tier_models(task.backend_config, tier_models or {})
    )
    effective_model = model or (tier_models or {}).get(0) or task.model
    effective_timeout = timeout or task.timeout_s
    trial_records: list[Trial] = []
    score_records: list[Score] = []
    for run_number in range(1, trials + 1):
        with _print_lock:
            tier_note = f" tiers=({_format_tier_models(tier_models)})" if tier_models else ""
            print(
                f"  · {task.id}: trial {run_number}/{trials} "
                f"(backend={backend} model={effective_model}{tier_note})…"
            )
        trial = runner.run_trial(
            task, run_number, subject=subject, model=effective_model, timeout_s=effective_timeout
        )
        trial_records.append(trial)
        score_records.append(
            graders.ungraded_score(task, trial)
            if skip_grading
            else graders.grade(task, trial, judge=judge)
        )
    return build_task_result(task, trial_records, score_records)


def _print_task_summary(task: Task) -> None:
    counts = {"deterministic": 0, "trajectory": 0, "llm": 0}
    for c in task.criteria:
        counts[c.grader] = counts.get(c.grader, 0) + 1
    ctrl = f"  control_for={task.control_for}" if task.control_for else ""
    counts_label = f"D/T/L={counts['deterministic']}/{counts['trajectory']}/{counts['llm']}"
    print(
        f"  {task.id:<26} {task.backend:<16} model={task.model:<14} diff={task.difficulty} "
        f"{task.type:<11} {counts_label}{ctrl}"
    )


# ── Commands ─────────────────────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace) -> int:
    if not args.path:
        suites_root = config.FRAMEWORK_ROOT / "suites"
        print("Available suites:")
        for d in sorted(p for p in suites_root.iterdir() if (p / "suite.toml").exists()):
            print(f"  {d.name}")
        return EXIT_OK

    meta, tasks = loader.load_suite(Path(args.path))
    print(f"Suite '{meta['name']}' — {meta.get('description', '')}")
    print(
        f"  default_backend={meta.get('default_backend', config.DEFAULT_BACKEND)}"
        f"  default_model={meta.get('default_model', config.DEFAULT_MODEL)}  tasks={len(tasks)}"
    )
    for task in tasks:
        _print_task_summary(task)
    return EXIT_OK


def cmd_task(args: argparse.Namespace) -> int:
    meta, task = loader.load_task_with_suite(Path(args.path))
    tier_models = _tier_model_overrides(args)
    if args.dry_run:
        print(f"[dry-run] would run {args.trials} trial(s):")
        _print_task_summary(task)
        if tier_models:
            print(f"  tier overrides: {_format_tier_models(tier_models)}")
        return EXIT_OK

    run_dir = config.RESULTS_DIR / _run_name(args.run_name)
    result = run_task(
        task,
        trials=args.trials,
        model=args.model,
        judge=_make_judge(meta, args),
        timeout=args.timeout,
        skip_grading=args.skip_grading,
        backend_override=args.backend,
        tier_models=tier_models,
    )
    reporter.save_task_artifacts(run_dir, result)
    reporter.print_task_result(result)
    print(f"\nArtifacts: {run_dir / task.id}")
    return _results_exit_code([result])


def _filter_suite_tasks(tasks: list[Task], args: argparse.Namespace) -> list[Task]:
    filtered = tasks
    if args.tags:
        tags = set(args.tags)
        filtered = [task for task in filtered if tags & set(task.tags)]
    if args.type:
        filtered = [task for task in filtered if task.type == args.type]
    return filtered


def cmd_suite(args: argparse.Namespace) -> int:
    meta, tasks = loader.load_suite(Path(args.path))
    tier_models = _tier_model_overrides(args)
    tasks = _filter_suite_tasks(tasks, args)
    if not tasks:
        print("No tasks matched the filters.")
        return EXIT_OK

    grader_model = args.grader_model or meta.get(
        "default_grader_model", config.DEFAULT_GRADER_MODEL
    )
    judge = _make_judge(meta, args)
    run_name = _run_name(args.run_name)
    run_dir = config.RESULTS_DIR / run_name
    started = datetime.now(UTC)

    if args.dry_run:
        print(
            f"[dry-run] suite '{meta['name']}' — {len(tasks)} task(s), {args.trials} trial(s) each:"
        )
        for task in tasks:
            _print_task_summary(task)
        if tier_models:
            print(f"  tier overrides: {_format_tier_models(tier_models)}")
        return EXIT_OK

    workers: int = getattr(args, "workers", 1)
    results: list[TaskResult]
    if workers > 1 and len(tasks) > 1:
        futures: list[Future[TaskResult]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for task in tasks:
                futures.append(
                    pool.submit(
                        _run_task_locked,
                        task,
                        trials=args.trials,
                        model=args.model,
                        judge=judge,
                        timeout=args.timeout,
                        skip_grading=args.skip_grading,
                        backend_override=args.backend,
                        tier_models=tier_models,
                    )
                )
        # Collect in original submission order so the summary table is stable.
        results = [f.result() for f in futures]
    else:
        results = []
        for task in tasks:
            results.append(
                run_task(
                    task,
                    trials=args.trials,
                    model=args.model,
                    judge=judge,
                    timeout=args.timeout,
                    skip_grading=args.skip_grading,
                    backend_override=args.backend,
                    tier_models=tier_models,
                )
            )

    token_values = [t.total_tokens for r in results for t in r.trials if t.total_tokens is not None]
    suite_result = SuiteResult(
        suite_name=str(meta["name"]),
        run_name=run_name,
        started_at=started,
        completed_at=datetime.now(UTC),
        model=_model_label(
            str(args.model or meta.get("default_model", config.DEFAULT_MODEL)),
            tier_models,
        ),
        grader_model=str(grader_model),
        harness_version=config.HARNESS_VERSION,
        trials_per_task=args.trials,
        task_results=results,
        total_cost_usd=sum(t.cost_usd or 0.0 for r in results for t in r.trials),
        total_tokens=sum(token_values) if token_values else None,
    )
    for result in results:
        reporter.save_task_artifacts(run_dir, result)
    reporter.save_run_metadata(run_dir, suite_result)
    for result in results:
        reporter.print_task_result(result)
    reporter.print_suite_summary(suite_result)
    print(f"Artifacts: {run_dir}")

    return _results_exit_code(results)


def cmd_score(args: argparse.Namespace) -> int:
    task_dir = Path(args.results_path).resolve()
    meta_path = task_dir / "task_metadata.json"
    if not meta_path.exists():
        print(f"No task_metadata.json in {task_dir}", file=sys.stderr)
        return EXIT_ERROR

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    suite_meta, task = loader.load_task_with_suite(Path(meta["path"]))
    judge = _make_judge(suite_meta, args)

    trials = reporter.load_archived_trials(task_dir)
    if not trials:
        print(f"No archived trials under {task_dir}", file=sys.stderr)
        return EXIT_ERROR
    scores = [graders.grade(task, trial, judge=judge) for trial in trials]
    result = build_task_result(task, trials, scores)
    print(
        f"Re-graded {len(trials)} archived trial(s) with current criteria "
        "without rerunning the subject:"
    )
    reporter.print_task_result(result)
    return EXIT_OK


def cmd_score_suite(args: argparse.Namespace) -> int:
    """Re-grade every task in an archived run directory against current criteria."""
    run_dir = Path(args.results_path).resolve()
    run_meta_path = run_dir / "run_metadata.json"
    if not run_meta_path.exists():
        print(f"No run_metadata.json in {run_dir}", file=sys.stderr)
        return EXIT_ERROR

    run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
    print(
        f"Re-grading run '{run_meta.get('run_name', run_dir.name)}' "
        f"(suite={run_meta.get('suite_name', '?')})  …"
    )

    task_dirs = sorted(
        p for p in run_dir.iterdir() if p.is_dir() and (p / "task_metadata.json").exists()
    )
    if not task_dirs:
        print(f"No graded task directories found under {run_dir}", file=sys.stderr)
        return EXIT_ERROR

    task_results: list[TaskResult] = []
    for task_dir in task_dirs:
        task_meta = json.loads((task_dir / "task_metadata.json").read_text(encoding="utf-8"))
        suite_meta, task = loader.load_task_with_suite(Path(task_meta["path"]))
        judge = _make_judge(suite_meta, args)
        trials = reporter.load_archived_trials(task_dir)
        if not trials:
            print(
                f"  ! {task.id}: no archived trials; re-grade aborted with artifacts unchanged",
                file=sys.stderr,
            )
            return EXIT_ERROR
        scores = [graders.grade(task, trial, judge=judge) for trial in trials]
        result = build_task_result(task, trials, scores)
        reporter.print_task_result(result)
        task_results.append(result)

    # Print aggregate summary
    total = len(task_results)
    passing = sum(1 for r in task_results if r.pass_at_k == 1.0)
    print(f"\nRe-grade complete: {passing}/{total} tasks pass@k=1.0")
    infra, infra_total, infra_rate = reporter.infra_error_stats(task_results)
    if infra_total > 0 and infra_rate >= config.INVALID_RUN_INFRA_RATE:
        print(
            f"  ⚠ RE-GRADE INVALID — {infra}/{infra_total} trials ({infra_rate:.0%}) "
            f"infra-failed (≥{config.INVALID_RUN_INFRA_RATE:.0%}). Archived scores, task "
            "metadata, and run metadata were left unchanged. Restore the failing "
            "infrastructure and re-grade."
        )
        return EXIT_ERROR

    for result in task_results:
        reporter.save_task_scores(run_dir, result)
    reporter.update_run_metadata_scores(run_dir, task_results)
    print(f"Artifacts updated in {run_dir}")
    return EXIT_OK


def _resolve_task_config(arg: str) -> Path:
    p = Path(arg)
    if p.suffix == ".toml" and p.exists():
        return p
    if (p / "config.toml").exists():
        return p / "config.toml"
    matches = sorted((config.FRAMEWORK_ROOT / "suites").glob(f"*/tasks/{arg}/config.toml"))
    if matches:
        return matches[0]
    raise LoaderError(f"could not resolve task config for {arg!r}")


def cmd_promote(args: argparse.Namespace) -> int:
    cfg_path = _resolve_task_config(args.task)
    task_id = cfg_path.parent.name
    suite_name = loader.load_suite_meta(cfg_path.parent.parent.parent)["name"]
    threshold = int(
        loader.load_suite_meta(cfg_path.parent.parent.parent).get(
            "promotion_threshold", config.DEFAULT_PROMOTION_THRESHOLD
        )
    )

    if not args.force:
        candidates = {
            c["task_id"] for c in ratchet.find_promotion_candidates(suite_name, threshold)
        }
        if task_id not in candidates:
            print(
                f"{task_id} is not a promotion candidate "
                f"(needs {threshold} consecutive clean runs). Use --force to override.",
                file=sys.stderr,
            )
            return EXIT_ERROR

    ratchet.update_task_type(
        cfg_path,
        "regression",
        fields_to_add={
            "promoted_at": date.today().isoformat(),
            "promotion_evidence": args.evidence or "manual promotion",
        },
    )
    print(f"Promoted {task_id} → regression.")
    return EXIT_OK


def cmd_demote(args: argparse.Namespace) -> int:
    cfg_path = _resolve_task_config(args.task)
    ratchet.update_task_type(
        cfg_path,
        "capability",
        fields_to_add={
            "demoted_at": date.today().isoformat(),
            "demotion_reason": args.reason,
        },
        remove_fields=["promoted_at", "promotion_evidence"],
    )
    print(f"Demoted {cfg_path.parent.name} → capability.")
    return EXIT_OK


def cmd_candidates(args: argparse.Namespace) -> int:
    if args.path:
        meta = loader.load_suite_meta(Path(args.path))
        suites = [
            (
                str(meta["name"]),
                int(meta.get("promotion_threshold", config.DEFAULT_PROMOTION_THRESHOLD)),
            )
        ]
    else:
        suites = []
        for d in sorted((config.FRAMEWORK_ROOT / "suites").iterdir()):
            if (d / "suite.toml").exists():
                m = loader.load_suite_meta(d)
                suites.append(
                    (
                        str(m["name"]),
                        int(m.get("promotion_threshold", config.DEFAULT_PROMOTION_THRESHOLD)),
                    )
                )

    found = False
    for name, threshold in suites:
        candidates = ratchet.find_promotion_candidates(name, threshold)
        for c in candidates:
            found = True
            print(
                f"  [{name}] {c['task_id']}: {c['consecutive_passes']} consecutive clean runs "
                f"(last: {c['last_run']}) — eligible for promotion"
            )
    if not found:
        print("No promotion candidates.")
    return EXIT_OK


def _load_json(path: Path) -> dict[str, Any]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _matrix_run_dirs(args: argparse.Namespace) -> list[Path]:
    if args.paths:
        dirs = [Path(p) for p in args.paths]
    else:
        dirs = sorted(config.RESULTS_DIR.glob("matrix-*"))
    return [d for d in dirs if (d / "run_metadata.json").exists()]


def _short_model_label(model: str) -> str:
    if "(" in model and model.endswith(")"):
        return model.split("(", 1)[1][:-1]
    return model


def _matrix_row(run_dir: Path) -> dict[str, Any]:
    meta = _load_json(run_dir / "run_metadata.json")
    task_meta = [_load_json(path) for path in sorted(run_dir.glob("*/task_metadata.json"))]
    task_meta = [task for task in task_meta if task]
    task_count = len(task_meta)
    metric_tasks = [
        task
        for task in task_meta
        if task.get("metrics_valid", True) is not False
        and isinstance(task.get("pass_at_k"), (int, float))
        and isinstance(task.get("pass_pow_k"), (int, float))
    ]
    metric_task_count = len(metric_tasks)
    pass_at_values = [float(task["pass_at_k"]) for task in metric_tasks]
    pass_pow_values = [float(task["pass_pow_k"]) for task in metric_tasks]
    zero_tasks = [str(task.get("id", "")) for task in task_meta if task.get("pass_pow_k") == 0.0]
    pass_at_full = sum(1 for task in metric_tasks if task.get("pass_at_k") == 1.0)
    pass_pow_full = sum(1 for task in metric_tasks if task.get("pass_pow_k") == 1.0)
    error_trials = 0
    sample_errors: list[str] = []
    for trial_path in sorted(run_dir.glob("*/trial-*/trial.json")):
        trial = _load_json(trial_path)
        error = trial.get("error")
        if error:
            error_trials += 1
            if len(sample_errors) < 3:
                sample_errors.append(str(error))
    return {
        "run": run_dir.name,
        "suite": str(meta.get("suite_name", "")),
        "model": _short_model_label(str(meta.get("model", ""))),
        "tasks": task_count,
        "metric_tasks": metric_task_count,
        "pass_at_full": pass_at_full,
        "pass_pow_full": pass_pow_full,
        "pass_pow_zero": len(zero_tasks),
        "mean_pass_at": sum(pass_at_values) / metric_task_count if metric_task_count else 0.0,
        "mean_pass_pow": sum(pass_pow_values) / metric_task_count if metric_task_count else 0.0,
        "wall_min": float(meta.get("wall_seconds") or 0.0) / 60.0,
        "agent_min": float(meta.get("agent_seconds_total") or 0.0) / 60.0,
        "tokens": int(meta.get("total_tokens") or 0),
        "error_trials": error_trials,
        "zero_tasks": ", ".join(zero_tasks),
        "sample_errors": " | ".join(sample_errors),
    }


def _print_matrix_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        "run",
        "suite",
        "model",
        "tasks",
        "pass@k",
        "pass^k",
        "zero",
        "mean@",
        "mean^",
        "wall",
        "tokens",
        "errors",
    ]
    table_rows = []
    for row in rows:
        table_rows.append(
            [
                row["run"],
                row["suite"],
                row["model"],
                str(row["tasks"]),
                f"{row['pass_at_full']}/{row['metric_tasks']}",
                f"{row['pass_pow_full']}/{row['metric_tasks']}",
                str(row["pass_pow_zero"]),
                f"{row['mean_pass_at']:.3f}",
                f"{row['mean_pass_pow']:.3f}",
                f"{row['wall_min']:.1f}m",
                str(row["tokens"]),
                str(row["error_trials"]),
            ]
        )
    widths = [
        min(48, max(len(str(cell)) for cell in [header, *[row[i] for row in table_rows]]))
        for i, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in table_rows:
        print("  ".join(str(cell)[: widths[i]].ljust(widths[i]) for i, cell in enumerate(row)))


def cmd_matrix(args: argparse.Namespace) -> int:
    run_dirs = _matrix_run_dirs(args)
    if not run_dirs:
        print("No completed matrix runs found.")
        return EXIT_OK
    rows = [_matrix_row(run_dir) for run_dir in run_dirs]
    rows.sort(key=lambda row: (row["suite"], row["model"], row["run"]))
    _print_matrix_table(rows)

    failures = [row for row in rows if row["pass_pow_zero"] or row["error_trials"]]
    if failures and not args.no_details:
        print("\nRuns needing attention:")
        for row in failures:
            print(f"- {row['run']}")
            if row["zero_tasks"]:
                print(f"  pass^k=0 tasks: {row['zero_tasks']}")
            if row["sample_errors"]:
                print(f"  sample errors: {row['sample_errors']}")
    return EXIT_OK


# ── argparse wiring ──────────────────────────────────────────────────────────


def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trials", type=int, default=config.DEFAULT_TRIALS)
    p.add_argument("--model", default=None)
    p.add_argument(
        "--tier-model",
        action="append",
        default=None,
        metavar="N=MODEL",
        help=(
            "override a Glassrail tier model for this run; repeatable, e.g. "
            "--tier-model 0=deepseek/deepseek-v4-flash --tier-model 1=deepseek/deepseek-v4-pro"
        ),
    )
    for tier in range(4):
        p.add_argument(
            f"--tier{tier}-model",
            default=None,
            help=f"convenience alias for --tier-model {tier}=MODEL",
        )
    p.add_argument(
        "--backend",
        default=None,
        help=f"override the subject backend ({', '.join(subjects.available_backends())})",
    )
    p.add_argument("--grader-model", default=None)
    p.add_argument(
        "--grader-backend", default=None, help="judge backend (claude-cli | openai-compat)"
    )
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-grading", action="store_true")
    p.add_argument("--run-name", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="AI skill eval framework")
    sub = parser.add_subparsers(dest="command", required=True)

    p_task = sub.add_parser("task", help="run a single task")
    p_task.add_argument("path")
    _add_run_flags(p_task)
    p_task.set_defaults(func=cmd_task)

    p_suite = sub.add_parser("suite", help="run a whole suite")
    p_suite.add_argument("path")
    _add_run_flags(p_suite)
    p_suite.add_argument("--tags", nargs="+", default=None)
    p_suite.add_argument("--type", choices=["regression", "capability"], default=None)
    p_suite.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="number of tasks to run in parallel (default: 1); tasks that share fixture"
        " paths are automatically serialised against each other",
    )
    p_suite.set_defaults(func=cmd_suite)

    p_list = sub.add_parser("list", help="validate + summarize a suite")
    p_list.add_argument("path", nargs="?", default=None)
    p_list.set_defaults(func=cmd_list)

    p_score = sub.add_parser("score", help="re-grade without rerunning the subject")
    p_score.add_argument("results_path")
    p_score.add_argument("--grader-model", default=None)
    p_score.add_argument(
        "--grader-backend", default=None, help="judge backend (claude-cli | openai-compat)"
    )
    p_score.set_defaults(func=cmd_score)

    p_score_suite = sub.add_parser("score-suite", help="re-grade all tasks in an archived run")
    p_score_suite.add_argument(
        "results_path", help="path to a run directory (contains run_metadata.json)"
    )
    p_score_suite.add_argument("--grader-model", default=None)
    p_score_suite.add_argument(
        "--grader-backend", default=None, help="judge backend (claude-cli | openai-compat)"
    )
    p_score_suite.set_defaults(func=cmd_score_suite)

    p_promote = sub.add_parser("promote", help="capability → regression")
    p_promote.add_argument("task")
    p_promote.add_argument("--evidence", default=None)
    p_promote.add_argument("--force", action="store_true")
    p_promote.set_defaults(func=cmd_promote)

    p_demote = sub.add_parser("demote", help="regression → capability")
    p_demote.add_argument("task")
    p_demote.add_argument("--reason", required=True)
    p_demote.set_defaults(func=cmd_demote)

    p_cand = sub.add_parser("candidates", help="list promotion candidates")
    p_cand.add_argument("path", nargs="?", default=None)
    p_cand.set_defaults(func=cmd_candidates)

    p_matrix = sub.add_parser("matrix", help="summarize completed matrix runs")
    p_matrix.add_argument(
        "paths",
        nargs="*",
        help="result run directories; defaults to every results/matrix-* run",
    )
    p_matrix.add_argument(
        "--no-details",
        action="store_true",
        help="suppress pass^k=0 task lists and sample endpoint errors",
    )
    p_matrix.set_defaults(func=cmd_matrix)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except LoaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
