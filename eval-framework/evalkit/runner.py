"""Runner — execute one trial: fixtures, subject invocation, evidence capture.

Each trial runs from a clean environment (principle 2). Any path named in
``fixtures.install`` or ``fixtures.capture`` is backed up to ``<path>.evalbackup``
before the run and restored in a ``finally`` — and stale backups from a prior
crash are restored before new fixtures are installed.

The runner is backend-agnostic: it hands a prompt to a :class:`~evalkit.subjects.base.Subject`
and records the normalized :class:`~evalkit.subjects.base.RunResult` into a
:class:`~evalkit.models.Trial`. It knows nothing about claude vs dagagent.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit import config
from evalkit.models import Task, Trial
from evalkit.subjects.base import Subject

_BACKUP_SUFFIX = ".evalbackup"


def _expand(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def _backup_path(path: Path) -> Path:
    return Path(str(path) + _BACKUP_SUFFIX)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _read_content(path: Path) -> str | None:
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return None


def _managed_paths(task: Task) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in [*task.fixtures.install.keys(), *task.fixtures.capture]:
        if raw not in seen:
            seen.add(raw)
            ordered.append(raw)
    return ordered


def _restore(path: Path) -> None:
    """Restore ``path`` from its backup, or delete it if it had none."""
    backup = _backup_path(path)
    if backup.exists():
        _remove(path)
        shutil.move(str(backup), str(path))
    else:
        _remove(path)  # didn't exist before the run; remove if created during it


def _backup(path: Path) -> None:
    backup = _backup_path(path)
    if backup.exists():
        _remove(backup)  # stale leftover
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, backup)
    elif path.exists():
        shutil.copy2(path, backup)


def _find_fixture_source(task: Task, source: str) -> Path:
    candidates = [
        task.path / "fixtures" / source,
        task.path.parent.parent / "shared-fixtures" / source,
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"fixture source {source!r} not found in {[str(c) for c in candidates]}")


def _install_fixtures(task: Task) -> None:
    for dest_raw, source in task.fixtures.install.items():
        dest = _expand(dest_raw)
        if source is None:
            _remove(dest)
            continue
        src = _find_fixture_source(task, source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _remove(dest)
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)


def _inject_scripted_path(task: Task, subject: Any) -> None:
    """If the subject is an exec-plan backend, resolve and set the scripted responses path.

    Looks for ``responses.jsonl`` in the task's fixtures directory.  When found,
    sets ``subject._scripted_path``; the subject propagates this to all tier env
    vars so THINK/reasoning_required nodes routed to tier 2 also get scripted
    responses.  No-ops for other backends (``_scripted_path`` is exec-plan-only).
    """
    if not hasattr(subject, "_scripted_path"):
        return
    responses = task.path / "fixtures" / "responses.jsonl"
    if not responses.exists():
        return
    subject._scripted_path = str(responses.resolve())


_EXEC_PLAN_PREFIX = "__EXEC_PLAN__"


def _build_prompt(task: Task) -> str:
    """Build the prompt string passed to the subject.

    For ``dagagent-exec-plan`` tasks the prompt.md contains a single line of
    the form ``__EXEC_PLAN__ fixtures/plan.json``.  This directive is resolved
    to an absolute path here so the subject receives a ready-to-use path
    without needing access to the task directory itself.
    """
    raw = task.prompt.strip()
    if raw.startswith(_EXEC_PLAN_PREFIX):
        # Directive: "__EXEC_PLAN__ fixtures/plan.json"
        # _find_fixture_source already prepends "fixtures/", so strip only that
        # literal prefix — not the first path component blindly.
        directive_path = raw[len(_EXEC_PLAN_PREFIX):].strip()
        _FIXTURES_PREFIX = "fixtures/"
        if directive_path.startswith(_FIXTURES_PREFIX):
            directive_path = directive_path[len(_FIXTURES_PREFIX):]
        return str(_find_fixture_source(task, directive_path).resolve())
    parts = [task.prompt]
    if task.context_files:
        block = ["\n\n## Context files\n"]
        for name, content in task.context_files.items():
            block.append(f"\n### {name}\n```\n{content}\n```\n")
        parts.append("".join(block))
    return "".join(parts)


def run_trial(task: Task, run_number: int, *, subject: Subject, model: str, timeout_s: int) -> Trial:
    """Run one trial end to end, always restoring fixtures in ``finally``."""
    started = datetime.now(UTC)
    managed = _managed_paths(task)
    baseline: dict[str, str | None] = {}
    side_effects: dict[str, str | None] = dict.fromkeys(task.fixtures.capture)

    error: str | None = None
    success = False
    result_text = ""
    trajectory: list[dict[str, Any]] = []
    envelope: dict[str, Any] = {}
    stdout = ""
    stderr = ""
    cost: float | None = None

    # Recover from any crash that left backups behind, then take fresh ones.
    for raw in managed:
        _restore(_expand(raw))

    try:
        for raw in managed:
            _backup(_expand(raw))
        for raw in task.fixtures.capture:
            baseline[raw] = _read_content(_expand(raw))

        _install_fixtures(task)
        _inject_scripted_path(task, subject)

        result = subject.run(
            prompt=_build_prompt(task),
            model=model,
            max_turns=task.max_turns,
            timeout_s=timeout_s,
        )
        stdout, stderr = result.raw_stdout, result.raw_stderr
        envelope = result.raw_envelope
        result_text = result.result_text
        trajectory = result.trajectory
        cost = result.cost_usd
        success = result.success
        error = result.error

        side_effects = {raw: _read_content(_expand(raw)) for raw in task.fixtures.capture}
    except Exception as exc:  # noqa: BLE001 - record any failure into the trial record
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for raw in managed:
            _restore(_expand(raw))

    completed = datetime.now(UTC)
    return Trial(
        task_id=task.id,
        run_number=run_number,
        started_at=started,
        completed_at=completed,
        success=success,
        error=error,
        duration_s=(completed - started).total_seconds(),
        output_envelope=envelope,
        result_text=result_text,
        raw_stdout=stdout,
        raw_stderr=stderr,
        trajectory=trajectory,
        side_effects=side_effects,
        cost_usd=cost,
        model=model,
        harness_version=config.HARNESS_VERSION,
        baseline=baseline,
    )
