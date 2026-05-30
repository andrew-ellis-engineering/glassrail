"""Load suites and tasks from TOML into the dataclass model.

Uses stdlib ``tomllib`` (Python 3.11+). Per-task config inherits defaults from
its ``suite.toml`` and finally from :mod:`evalkit.config`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from evalkit import config
from evalkit.models import Criterion, FixtureSpec, Task


class LoaderError(Exception):
    """Raised when a suite or task fails to parse or validate."""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise LoaderError(f"missing TOML file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise LoaderError(f"invalid TOML in {path}: {exc}") from exc


def load_suite_meta(suite_dir: Path) -> dict[str, Any]:
    """Read and lightly validate ``<suite_dir>/suite.toml``'s ``[suite]`` table.

    Optional ``[backend]`` and ``[judge]`` tables (subject / judge wiring) are
    folded into the returned meta dict under ``backend_config`` / ``judge_config``.
    """
    data = _read_toml(suite_dir / "suite.toml")
    raw = data.get("suite", {})
    if not isinstance(raw, dict) or "name" not in raw:
        raise LoaderError(f"{suite_dir}/suite.toml needs a [suite] table with a name")
    meta = dict(raw)
    if isinstance(data.get("backend"), dict):
        meta["backend_config"] = data["backend"]
    if isinstance(data.get("judge"), dict):
        meta["judge_config"] = data["judge"]
    return meta


def _parse_criteria(raw: list[dict[str, Any]], task_dir: Path) -> list[Criterion]:
    criteria: list[Criterion] = []
    for i, c in enumerate(raw):
        if "text" not in c or "grader" not in c:
            raise LoaderError(f"{task_dir}: criterion #{i + 1} needs 'text' and 'grader'")
        grader = c["grader"]
        if grader not in ("deterministic", "trajectory", "llm"):
            raise LoaderError(f"{task_dir}: criterion '{c['text']}' has unknown grader '{grader}'")
        criteria.append(
            Criterion(
                text=str(c["text"]),
                grader=str(grader),
                check=c.get("check"),
                target=c.get("target"),
                value=c.get("value"),
                tool_sequence=c.get("tool_sequence"),
            )
        )
    return criteria


def _parse_fixtures(raw: dict[str, Any]) -> FixtureSpec:
    install_raw = raw.get("install", {}) or {}
    # Empty string in TOML means "delete this path before the run"; normalise
    # to None so the runner has one sentinel for deletion.
    install: dict[str, str | None] = {
        dest: (None if src == "" else str(src)) for dest, src in install_raw.items()
    }
    capture = [str(p) for p in raw.get("capture", []) or []]
    return FixtureSpec(install=install, capture=capture)


def load_task(task_dir: Path, suite_meta: dict[str, Any], suite_name: str) -> Task:
    """Load a single task directory into a :class:`Task`."""
    task_dir = task_dir.resolve()
    cfg = _read_toml(task_dir / "config.toml")
    prompt_path = task_dir / "prompt.md"
    if not prompt_path.exists():
        raise LoaderError(f"{task_dir}: missing prompt.md")
    prompt = prompt_path.read_text(encoding="utf-8")

    model = str(cfg.get("model") or suite_meta.get("default_model") or config.DEFAULT_MODEL)
    timeout_s = int(
        cfg.get("timeout_s") or suite_meta.get("default_timeout_s") or config.DEFAULT_TIMEOUT_S
    )
    max_turns = int(cfg.get("max_turns") or config.DEFAULT_MAX_TURNS)

    fixtures = _parse_fixtures(cfg.get("fixtures", {}) or {})
    criteria = _parse_criteria(cfg.get("criteria", []) or [], task_dir)
    context_files = {str(k): str(v) for k, v in (cfg.get("context_files", {}) or {}).items()}

    backend = str(cfg.get("backend") or suite_meta.get("default_backend") or config.DEFAULT_BACKEND)
    backend_config: dict[str, Any] = {
        **(suite_meta.get("backend_config") or {}),
        **(cfg.get("backend_config") or {}),
    }

    return Task(
        id=task_dir.name,
        name=str(cfg.get("name", task_dir.name)),
        suite=suite_name,
        path=task_dir,
        prompt=prompt,
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        tags=[str(t) for t in cfg.get("tags", []) or []],
        type=str(cfg.get("type", "capability")),
        difficulty=int(cfg.get("difficulty", 1)),
        control_for=cfg.get("control_for"),
        expected_behavior=str(cfg.get("expected_behavior", "")),
        criteria=criteria,
        fixtures=fixtures,
        context_files=context_files,
        backend=backend,
        backend_config=backend_config,
    )


def load_suite(suite_dir: Path) -> tuple[dict[str, Any], list[Task]]:
    """Load a suite directory: its metadata and all tasks under ``tasks/``."""
    suite_dir = suite_dir.resolve()
    meta = load_suite_meta(suite_dir)
    tasks_root = suite_dir / "tasks"
    if not tasks_root.is_dir():
        raise LoaderError(f"{suite_dir}: missing tasks/ directory")
    tasks: list[Task] = []
    for task_dir in sorted(p for p in tasks_root.iterdir() if p.is_dir()):
        if (task_dir / "config.toml").exists():
            tasks.append(load_task(task_dir, meta, str(meta["name"])))
    return meta, tasks


def load_task_with_suite(task_dir: Path) -> tuple[dict[str, Any], Task]:
    """Load a standalone task path, locating its ``suite.toml`` two levels up."""
    task_dir = task_dir.resolve()
    suite_dir = task_dir.parent.parent  # tasks/<task>/ -> suite root
    meta = load_suite_meta(suite_dir)
    return meta, load_task(task_dir, meta, str(meta["name"]))
