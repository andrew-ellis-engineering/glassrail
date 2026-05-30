#!/usr/bin/env python3
"""Eval framework CLI.

Subcommands: task, suite, list, score, promote, demote, candidates.

Exit codes: 0 = success, 1 = a regression task had pass^k = 0 (CI gating
signal), 2 = framework error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
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
    n = len(trials)
    perfect = sum(1 for s in scores if s.pass_rate == 1.0)
    return TaskResult(
        task=task,
        trials=trials,
        scores=scores,
        pass_at_k=stats.pass_at_k(n, perfect, n) if n else 0.0,
        pass_pow_k=stats.pass_pow_k(scores),
        mean_pass_rate=stats.mean_pass_rate(scores),
    )


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


def run_task(
    task: Task,
    *,
    trials: int,
    model: str | None,
    judge: Judge,
    timeout: int | None,
    skip_grading: bool,
    backend_override: str | None = None,
) -> TaskResult:
    backend = backend_override or task.backend
    subject = subjects.build_subject(backend, task.backend_config)
    effective_model = model or task.model
    effective_timeout = timeout or task.timeout_s
    trial_records: list[Trial] = []
    score_records: list[Score] = []
    for run_number in range(1, trials + 1):
        print(f"  · {task.id}: trial {run_number}/{trials} (backend={backend} model={effective_model})…")
        trial = runner.run_trial(
            task, run_number, subject=subject, model=effective_model, timeout_s=effective_timeout
        )
        trial_records.append(trial)
        if not skip_grading:
            score_records.append(graders.grade(task, trial, judge=judge))
    return build_task_result(task, trial_records, score_records)


def _print_task_summary(task: Task) -> None:
    counts = {"deterministic": 0, "trajectory": 0, "llm": 0}
    for c in task.criteria:
        counts[c.grader] = counts.get(c.grader, 0) + 1
    ctrl = f"  control_for={task.control_for}" if task.control_for else ""
    print(
        f"  {task.id:<26} {task.backend:<16} model={task.model:<14} diff={task.difficulty} "
        f"{task.type:<11} D/T/L={counts['deterministic']}/{counts['trajectory']}/{counts['llm']}{ctrl}"
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
    if args.dry_run:
        print(f"[dry-run] would run {args.trials} trial(s):")
        _print_task_summary(task)
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
    )
    reporter.save_task_artifacts(run_dir, result)
    if not args.skip_grading:
        reporter.print_task_result(result)
    print(f"\nArtifacts: {run_dir / task.id}")
    if task.type == "regression" and result.pass_pow_k == 0.0:
        return EXIT_REGRESSION
    return EXIT_OK


def cmd_suite(args: argparse.Namespace) -> int:
    meta, tasks = loader.load_suite(Path(args.path))
    if args.tags:
        tags = set(args.tags)
        tasks = [t for t in tasks if tags & set(t.tags)]
    if args.type:
        tasks = [t for t in tasks if t.type == args.type]
    if not tasks:
        print("No tasks matched the filters.")
        return EXIT_OK

    grader_model = args.grader_model or meta.get("default_grader_model", config.DEFAULT_GRADER_MODEL)
    judge = _make_judge(meta, args)
    run_name = _run_name(args.run_name)
    run_dir = config.RESULTS_DIR / run_name
    started = datetime.now(UTC)

    if args.dry_run:
        print(f"[dry-run] suite '{meta['name']}' — {len(tasks)} task(s), {args.trials} trial(s) each:")
        for task in tasks:
            _print_task_summary(task)
        return EXIT_OK

    results: list[TaskResult] = []
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
            )
        )

    suite_result = SuiteResult(
        suite_name=str(meta["name"]),
        run_name=run_name,
        started_at=started,
        completed_at=datetime.now(UTC),
        model=str(args.model or meta.get("default_model", config.DEFAULT_MODEL)),
        grader_model=str(grader_model),
        harness_version=config.HARNESS_VERSION,
        trials_per_task=args.trials,
        task_results=results,
        total_cost_usd=sum(t.cost_usd or 0.0 for r in results for t in r.trials),
    )
    for result in results:
        reporter.save_task_artifacts(run_dir, result)
    reporter.save_run_metadata(run_dir, suite_result)
    if not args.skip_grading:
        for result in results:
            reporter.print_task_result(result)
        reporter.print_suite_summary(suite_result)
    print(f"Artifacts: {run_dir}")

    regression_failed = any(
        r.task.type == "regression" and r.pass_pow_k == 0.0 for r in results
    )
    return EXIT_REGRESSION if regression_failed else EXIT_OK


def cmd_score(args: argparse.Namespace) -> int:
    task_dir = Path(args.results_path).resolve()
    meta_path = task_dir / "task_metadata.json"
    if not meta_path.exists():
        print(f"No task_metadata.json in {task_dir}", file=sys.stderr)
        return EXIT_ERROR

    import json

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    suite_meta, task = loader.load_task_with_suite(Path(meta["path"]))
    judge = _make_judge(suite_meta, args)

    trials = reporter.load_archived_trials(task_dir)
    if not trials:
        print(f"No archived trials under {task_dir}", file=sys.stderr)
        return EXIT_ERROR
    scores = [graders.grade(task, trial, judge=judge) for trial in trials]
    result = build_task_result(task, trials, scores)
    print(f"Re-graded {len(trials)} archived trial(s) with current criteria (zero inference):")
    reporter.print_task_result(result)
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
        candidates = {c["task_id"] for c in ratchet.find_promotion_candidates(suite_name, threshold)}
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
        suites = [(str(meta["name"]), int(meta.get("promotion_threshold", config.DEFAULT_PROMOTION_THRESHOLD)))]
    else:
        suites = []
        for d in sorted((config.FRAMEWORK_ROOT / "suites").iterdir()):
            if (d / "suite.toml").exists():
                m = loader.load_suite_meta(d)
                suites.append((str(m["name"]), int(m.get("promotion_threshold", config.DEFAULT_PROMOTION_THRESHOLD))))

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


# ── argparse wiring ──────────────────────────────────────────────────────────


def _add_run_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trials", type=int, default=config.DEFAULT_TRIALS)
    p.add_argument("--model", default=None)
    p.add_argument(
        "--backend",
        default=None,
        help=f"override the subject backend ({', '.join(subjects.available_backends())})",
    )
    p.add_argument("--grader-model", default=None)
    p.add_argument("--grader-backend", default=None, help="judge backend (claude-cli | openai-compat)")
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
    p_suite.set_defaults(func=cmd_suite)

    p_list = sub.add_parser("list", help="validate + summarize a suite")
    p_list.add_argument("path", nargs="?", default=None)
    p_list.set_defaults(func=cmd_list)

    p_score = sub.add_parser("score", help="re-grade archived trials (zero inference)")
    p_score.add_argument("results_path")
    p_score.add_argument("--grader-model", default=None)
    p_score.add_argument("--grader-backend", default=None, help="judge backend (claude-cli | openai-compat)")
    p_score.set_defaults(func=cmd_score)

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
