"""Promotion ratchet — capability → regression after proven stability.

A capability task qualifies when its last ``threshold`` consecutive suite runs
all had pass^k = 1.0 (principle 9). Promotion is reported, never automatic — a
human runs ``run.py promote``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from evalkit import config


def _load_runs_for_suite(suite_name: str, results_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(started_at, run_dir)`` for each run of ``suite_name``, oldest first."""
    runs: list[tuple[str, Path]] = []
    if not results_dir.is_dir():
        return runs
    for run_dir in results_dir.iterdir():
        meta_path = run_dir / "run_metadata.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("suite_name") == suite_name:
            runs.append((str(meta.get("started_at", run_dir.name)), run_dir))
    runs.sort(key=lambda pair: pair[0])
    return runs


def _task_meta(run_dir: Path, task_id: str) -> dict[str, Any] | None:
    meta_path = run_dir / task_id / "task_metadata.json"
    if not meta_path.exists():
        return None
    return json.loads(meta_path.read_text(encoding="utf-8"))


def find_promotion_candidates(
    suite_name: str, threshold: int, results_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Tasks whose last ``threshold`` consecutive runs all had pass^k = 1.0."""
    results_dir = results_dir or config.RESULTS_DIR
    runs = _load_runs_for_suite(suite_name, results_dir)
    if not runs:
        return []

    task_ids: set[str] = set()
    for _, run_dir in runs:
        task_ids.update(p.name for p in run_dir.iterdir() if (p / "task_metadata.json").exists())

    candidates: list[dict[str, Any]] = []
    for task_id in sorted(task_ids):
        consecutive = 0
        for _, run_dir in reversed(runs):  # newest first
            meta = _task_meta(run_dir, task_id)
            if meta is not None and meta.get("pass_pow_k") == 1.0:
                consecutive += 1
            else:
                break
        latest_meta = next(
            (_task_meta(rd, task_id) for _, rd in reversed(runs) if _task_meta(rd, task_id)), None
        )
        is_capability = (latest_meta or {}).get("type") == "capability"
        if consecutive >= threshold and is_capability:
            candidates.append(
                {
                    "task_id": task_id,
                    "consecutive_passes": consecutive,
                    "last_run": runs[-1][1].name,
                }
            )
    return candidates


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))  # JSON string == TOML basic string for our cases


def update_task_type(
    config_path: Path,
    new_type: str,
    fields_to_add: dict[str, Any] | None = None,
    remove_fields: list[str] | None = None,
) -> None:
    """Surgically edit a task ``config.toml``: set ``type`` and add/remove keys.

    Regex-based to preserve formatting and comments. New keys are inserted next
    to the ``type`` line so they stay in the top-level table (before any
    ``[fixtures]`` / ``[[criteria]]`` header).
    """
    fields_to_add = fields_to_add or {}
    remove_fields = remove_fields or []

    def is_key(line: str, key: str) -> bool:
        return re.match(rf"^\s*{re.escape(key)}\s*=", line) is not None

    lines = [
        ln for ln in config_path.read_text(encoding="utf-8").splitlines()
        if not any(is_key(ln, k) for k in remove_fields)
    ]

    type_idx: int | None = None
    for i, ln in enumerate(lines):
        if is_key(ln, "type"):
            lines[i] = f'type = "{new_type}"'
            type_idx = i
    if type_idx is None:
        lines.insert(0, f'type = "{new_type}"')
        type_idx = 0

    for key, value in fields_to_add.items():
        formatted = f"{key} = {_toml_scalar(value)}"
        replaced = False
        for j, ln in enumerate(lines):
            if is_key(ln, key):
                lines[j] = formatted
                replaced = True
                break
        if not replaced:
            lines.insert(type_idx + 1, formatted)

    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
