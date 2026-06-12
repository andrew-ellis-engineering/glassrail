"""Tests for the Typer CLI surface."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

from glassrail import __version__
from glassrail.cli import app
from glassrail.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _scripted_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {
        "GLASSRAIL_CONFIG_HOME": "/nonexistent/glassrail-cli-test",
        "GLASSRAIL_PLANNER_MIN_TIER": "0",
    }
    for tier in range(4):
        env[f"GLASSRAIL_TIER{tier}__KIND"] = "scripted"
        env[f"GLASSRAIL_TIER{tier}__SCRIPTED_PATH"] = str(path)
    return env


def _json_output(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    return _parse_json_output(result)


def _parse_json_output(result: Any) -> dict[str, Any]:
    data = json.loads(result.output)
    assert isinstance(data, dict)
    return data


def test_version_command_prints_package_version() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_run_json_emits_contract_envelope(tmp_path: Path) -> None:
    plan = {
        "nodes": [
            {
                "id": 1,
                "type": "result",
                "description": "Answer the user directly",
            }
        ]
    }
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        "\n".join(
            [
                json.dumps(plan),
                json.dumps({"output": "cli golden result", "confidence": 0.91}),
            ]
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["run", "produce a deterministic CLI result", "--json"],
        env=_scripted_env(responses),
    )

    data = _json_output(result)
    assert set(data) == {
        "result",
        "trajectory",
        "status",
        "is_error",
        "error",
        "total_cost_usd",
        "total_tokens",
        "task_id",
        "replan_count",
        "plan",
        "planning_attempts",
        "branch_log",
        "flagged_nodes",
    }
    assert data["result"] == "cli golden result"
    assert data["status"] == "completed"
    assert data["is_error"] is False
    assert data["error"] is None
    assert isinstance(data["trajectory"], list)
    assert isinstance(data["total_tokens"], int)
    assert isinstance(data["task_id"], str)
    assert isinstance(data["replan_count"], int)
    assert isinstance(data["plan"], dict)
    assert isinstance(data["planning_attempts"], list)
    assert isinstance(data["branch_log"], list)
    assert isinstance(data["flagged_nodes"], list)
    assert data["trajectory"][0]["tool"] == "result"
    assert data["trajectory"][0]["node_type"] == "result"
    assert data["trajectory"][0]["status"] == "completed"


def test_run_json_exits_one_with_parseable_failure_envelope(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text("not json\nstill not json\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["run", "produce a deterministic CLI failure", "--json"],
        env=_scripted_env(responses),
    )

    assert result.exit_code == 1
    data = _parse_json_output(result)
    assert data["status"] == "failed"
    assert data["is_error"] is True
    assert isinstance(data["planning_attempts"], list)


def test_exec_plan_json_runs_harness_mechanics_fixture(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    fixture_dir = (
        root / "eval-framework" / "suites" / "harness-mechanics" / "tasks" / "llm-output-key-result"
    )
    plan = fixture_dir / "fixtures" / "plan.json"
    responses = fixture_dir / "fixtures" / "responses.jsonl"
    monkeypatch.chdir(root)
    runner = CliRunner()

    result = runner.invoke(app, ["exec-plan", str(plan), "--json"], env=_scripted_env(responses))

    data = _json_output(result)
    assert data["result"] == "scripted result sentinel"
    assert data["status"] == "completed"
    assert data["is_error"] is False
    assert data["trajectory"][0]["tool"] == "result"
    assert data["trajectory"][0]["output"] == "scripted result sentinel"


def test_exec_plan_json_exits_one_with_parseable_failure_envelope(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["exec-plan", str(plan), "--json"])

    assert result.exit_code == 1
    data = _parse_json_output(result)
    assert data["status"] == "failed"
    assert data["is_error"] is True
    assert "plan parse failed" in str(data["error"])


def test_tui_acp_and_serve_help_render() -> None:
    runner = CliRunner()

    tui = runner.invoke(app, ["tui", "--help"])
    acp = runner.invoke(app, ["acp", "--help"])
    serve = runner.invoke(app, ["serve", "--help"])
    root = runner.invoke(app, ["--help"])

    assert tui.exit_code == 0
    assert "Submit a task to a running gateway" in tui.output
    assert acp.exit_code == 0
    assert "Agent Client Protocol" in acp.output
    assert serve.exit_code == 0
    assert "Serve the REST gateway" in serve.output
    assert root.exit_code == 0
    assert "serve" in root.output
