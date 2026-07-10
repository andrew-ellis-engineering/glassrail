from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import run
from evalkit import config, graders, ratchet, reporter, runner
from evalkit.models import (
    Criterion,
    CriterionResult,
    FixtureSpec,
    Score,
    SuiteResult,
    Task,
    Trial,
)
from evalkit.subjects.glassrail_cli import _result_from_proc
from evalkit.subjects.glassrail_gateway import result_from_state


def _task(path: Path, *, task_type: str = "capability", grader: str = "llm") -> Task:
    return Task(
        id="integrity-task",
        name="Integrity task",
        suite="integrity",
        path=path,
        prompt="Answer the task.",
        model="test-model",
        max_turns=1,
        timeout_s=1,
        tags=[],
        type=task_type,
        difficulty=1,
        control_for=None,
        expected_behavior="A correct answer.",
        criteria=[Criterion(text="answer is correct", grader=grader)],
        fixtures=FixtureSpec(install={}, capture=[]),
        context_files={},
    )


def _trial(
    *,
    run_number: int = 1,
    success: bool = True,
    error: str | None = None,
    result_text: str = "answer",
    output_envelope: dict[str, object] | None = None,
    infra_error: bool = False,
) -> Trial:
    now = datetime.now(UTC)
    return Trial(
        task_id="integrity-task",
        run_number=run_number,
        started_at=now,
        completed_at=now,
        success=success,
        error=error,
        duration_s=0.01,
        output_envelope=output_envelope or {},
        result_text=result_text,
        raw_stdout="",
        raw_stderr="",
        trajectory=[],
        side_effects={},
        cost_usd=None,
        total_tokens=None,
        model="test-model",
        harness_version=config.HARNESS_VERSION,
        infra_error=infra_error,
    )


def _score(run_number: int, *, passed: bool, infra_error: bool = False) -> Score:
    criterion = CriterionResult(
        criterion_text="answer is correct",
        passed=passed,
        evidence="fixture score",
        grader_used="llm",
        infra_error=infra_error,
    )
    return Score(
        task_id="integrity-task",
        trial_num=run_number,
        criterion_results=[criterion],
        passed=int(passed),
        failed=int(not passed),
        total=1,
        pass_rate=float(passed),
        infra_error=infra_error,
    )


class _CrashingSubject:
    def run(self, **_kwargs: object) -> object:
        raise NameError("subject exploded")


class InfrastructureIntegrityTests(unittest.TestCase):
    def test_invalid_run_breaks_promotion_streak(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            results_dir = Path(temp_dir)
            for index in range(1, 7):
                run_dir = results_dir / f"run-{index}"
                task_dir = run_dir / "integrity-task"
                task_dir.mkdir(parents=True)
                (run_dir / "run_metadata.json").write_text(
                    json.dumps(
                        {
                            "suite_name": "integrity",
                            "started_at": f"2026-07-{index:02d}",
                            "flagged_invalid": index == 6,
                        }
                    ),
                    encoding="utf-8",
                )
                (task_dir / "task_metadata.json").write_text(
                    json.dumps(
                        {
                            "id": "integrity-task",
                            "type": "capability",
                            "pass_pow_k": 1.0,
                            "metrics_valid": True,
                        }
                    ),
                    encoding="utf-8",
                )

            candidates = ratchet.find_promotion_candidates(
                "integrity",
                threshold=5,
                results_dir=results_dir,
            )

        self.assertEqual(candidates, [])

    def test_gateway_token_total_includes_planning_and_execution(self) -> None:
        result = result_from_state(
            {
                "status": "completed",
                "final_output": "answer",
                "planning_attempts": [
                    {"tokens_used": 11},
                    {"tokens_used": 13},
                ],
                "results": {
                    "1": {"tokens_used": 17},
                    "2": {"tokens_used": 19},
                },
            }
        )

        self.assertEqual(result.total_tokens, 60)
        self.assertEqual(result.raw_envelope["total_tokens"], 60)

    def test_runner_marks_arbitrary_subject_crash_as_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = _task(Path(temp_dir))
            trial = runner.run_trial(
                task,
                1,
                subject=_CrashingSubject(),
                model="test-model",
                timeout_s=1,
            )

        self.assertFalse(trial.success)
        self.assertTrue(trial.infra_error)
        self.assertIn("NameError", trial.error or "")
        self.assertTrue(graders.trial_infra_error(trial))

    def test_parseable_subject_and_judge_failures_remain_model_quality(self) -> None:
        envelope = {"is_error": True, "status": "failed", "trajectory": []}
        subject_result = _result_from_proc(1, json.dumps(envelope), "")
        self.assertFalse(subject_result.infra_error)

        with tempfile.TemporaryDirectory() as temp_dir:
            task = _task(Path(temp_dir))
            trial = _trial(
                success=False,
                error="glassrail task failed",
                result_text="",
                output_envelope=envelope,
            )
            score = graders.grade(task, trial, judge=lambda *_args, **_kwargs: "FAIL\nwrong")
            unknown = graders.grade(
                task, _trial(), judge=lambda *_args, **_kwargs: "UNKNOWN\ninsufficient"
            )

        self.assertFalse(graders.trial_infra_error(trial))
        self.assertFalse(score.infra_error)
        self.assertEqual(score.pass_rate, 0.0)
        self.assertFalse(unknown.infra_error)

    def test_unreachable_judge_is_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            score = graders.grade(
                _task(Path(temp_dir)), _trial(), judge=lambda *_args, **_kwargs: None
            )

        self.assertTrue(score.infra_error)
        self.assertTrue(score.criterion_results[0].infra_error)
        self.assertEqual(score.criterion_results[0].evidence, "judge invocation failed")

    def test_metrics_exclude_infrastructure_and_invalidate_when_none_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = _task(Path(temp_dir))
            trials = [_trial(run_number=1), _trial(run_number=2, infra_error=True)]
            result = run.build_task_result(
                task,
                trials,
                [_score(1, passed=True), _score(2, passed=False, infra_error=True)],
            )
            invalid = run.build_task_result(
                task,
                [trials[1]],
                [_score(2, passed=False, infra_error=True)],
            )

        self.assertEqual(result.pass_at_k, 1.0)
        self.assertEqual(result.pass_pow_k, 1.0)
        self.assertEqual(result.mean_pass_rate, 1.0)
        self.assertIsNone(invalid.pass_at_k)
        self.assertIsNone(invalid.pass_pow_k)
        self.assertIsNone(invalid.mean_pass_rate)

    def test_below_and_at_threshold_have_distinct_gate_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = _task(Path(temp_dir), task_type="regression")
            below_trials = [_trial(run_number=i) for i in range(1, 7)]
            below_scores = [_score(1, passed=False, infra_error=True)] + [
                _score(i, passed=False) for i in range(2, 7)
            ]
            below = run.build_task_result(task, below_trials, below_scores)

            at_trials = [_trial(run_number=i) for i in range(1, 6)]
            at_scores = [_score(1, passed=False, infra_error=True)] + [
                _score(i, passed=True) for i in range(2, 6)
            ]
            at = run.build_task_result(task, at_trials, at_scores)

        self.assertFalse(reporter.run_is_invalid([below]))
        self.assertEqual(run._results_exit_code([below]), run.EXIT_REGRESSION)
        self.assertTrue(reporter.run_is_invalid([at]))
        self.assertEqual(run._results_exit_code([at]), run.EXIT_ERROR)

    def test_skip_grading_still_records_runtime_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            task = _task(Path(temp_dir))
            with patch.object(run.subjects, "build_subject", return_value=_CrashingSubject()):
                result = run.run_task(
                    task,
                    trials=1,
                    model=None,
                    judge=lambda *_args, **_kwargs: self.fail("judge must not run"),
                    timeout=None,
                    skip_grading=True,
                )

        self.assertEqual(len(result.scores), 1)
        self.assertFalse(result.scores[0].graded)
        self.assertTrue(result.scores[0].infra_error)
        self.assertIsNone(result.pass_pow_k)

    def test_invalid_suite_regrade_preserves_archive_then_valid_regrade_updates_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "archived-run"
            task = _task(root)
            trial = _trial()
            archived = run.build_task_result(task, [trial], [_score(1, passed=True)])
            suite = SuiteResult(
                suite_name="integrity",
                run_name="archived-run",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                model="test-model",
                grader_model="test-judge",
                harness_version="archived-version",
                trials_per_task=1,
                task_results=[archived],
                total_cost_usd=0.0,
            )
            reporter.save_task_artifacts(run_dir, archived)
            reporter.save_run_metadata(run_dir, suite)

            task_meta_path = run_dir / task.id / "task_metadata.json"
            score_path = run_dir / task.id / "trial-01" / "score.json"
            run_meta_path = run_dir / "run_metadata.json"
            before = {
                path: path.read_bytes() for path in (task_meta_path, score_path, run_meta_path)
            }
            args = Namespace(
                results_path=str(run_dir), grader_model=None, grader_backend=None
            )

            output = StringIO()
            with (
                patch.object(run.loader, "load_task_with_suite", return_value=({}, task)),
                patch.object(run, "_make_judge", return_value=lambda *_args, **_kwargs: None),
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                invalid_exit = run.cmd_score_suite(args)

            self.assertEqual(invalid_exit, run.EXIT_ERROR)
            self.assertIn("left unchanged", output.getvalue())
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)

            with (
                patch.object(run.loader, "load_task_with_suite", return_value=({}, task)),
                patch.object(
                    run,
                    "_make_judge",
                    return_value=lambda *_args, **_kwargs: "PASS\ncorrect",
                ),
                redirect_stdout(StringIO()),
            ):
                valid_exit = run.cmd_score_suite(args)

            self.assertEqual(valid_exit, run.EXIT_OK)
            self.assertNotEqual(score_path.read_bytes(), before[score_path])
            run_metadata = json.loads(run_meta_path.read_text(encoding="utf-8"))
            task_metadata = json.loads(task_meta_path.read_text(encoding="utf-8"))
            self.assertFalse(run_metadata["flagged_invalid"])
            self.assertEqual(run_metadata["scoring_harness_version"], config.HARNESS_VERSION)
            self.assertEqual(run_metadata["model_quality_trials"], 1)
            self.assertEqual(task_metadata["model_quality_trials"], 1)


if __name__ == "__main__":
    unittest.main()
