"""Trajectory grader — tool-call evidence (principle 4).

Presence mode (default): the expected ``tool_sequence`` must appear as a
subsequence (order preserved, gaps allowed). If ``target`` is given, one of
those tool calls must reference that path.

Absent mode (``value = "absent"``): NONE of ``tool_sequence`` may appear — or,
if no ``tool_sequence`` is given, the trajectory must be entirely empty. This
is the primary check for no-tool commands; never mistake it for a missing
``tool_sequence`` error (gotcha: check ``value`` first).
"""

from __future__ import annotations

import os
from typing import Any

from evalkit.models import Criterion, CriterionResult, Trial


def _result(criterion: Criterion, passed: bool, evidence: str) -> CriterionResult:
    return CriterionResult(
        criterion_text=criterion.text,
        passed=passed,
        evidence=evidence,
        grader_used="trajectory",
    )


def _tool_names(trajectory: list[dict[str, Any]]) -> list[str]:
    return [str(step.get("tool", "")) for step in trajectory]


def _is_subsequence(expected: list[str], actual: list[str]) -> bool:
    it = iter(actual)
    return all(tool in it for tool in expected)


def _references_target(trajectory: list[dict[str, Any]], tools: list[str], target: str) -> bool:
    """True if any call to one of ``tools`` mentions ``target`` in its input."""
    candidates = {target, os.path.expanduser(target), os.path.basename(target)}
    for step in trajectory:
        if str(step.get("tool", "")) not in tools:
            continue
        for val in _flatten_strings(step.get("input", {})):
            if any(c and c in val for c in candidates):
                return True
    return False


def _flatten_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_flatten_strings(v))
    return out


def grade(criterion: Criterion, trial: Trial) -> CriterionResult:
    names = _tool_names(trial.trajectory)
    seq = criterion.tool_sequence or []

    # Absent mode — check value FIRST so an empty sequence isn't an error.
    if criterion.value == "absent":
        if not seq:
            ok = len(names) == 0
            return _result(criterion, ok, "no tools called" if ok else f"tools used: {names}")
        present = [t for t in seq if t in names]
        return _result(
            criterion, not present, "forbidden tools absent" if not present else f"used: {present}"
        )

    # Presence mode.
    if not seq:
        return _result(criterion, False, "trajectory presence check requires tool_sequence")
    if not _is_subsequence(seq, names):
        return _result(criterion, False, f"expected subsequence {seq} not found in {names}")
    if criterion.target is not None and not _references_target(trial.trajectory, seq, criterion.target):
        return _result(criterion, False, f"no {seq} call referenced {criterion.target}")
    return _result(criterion, True, f"{seq} present" + (f" on {criterion.target}" if criterion.target else ""))
