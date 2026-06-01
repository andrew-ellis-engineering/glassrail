"""Deterministic graders — code-based checks, 100% precision (principle 1).

These run first and decide on their own; an LLM judge never overrides a
deterministic verdict. Safety-critical checks belong here.
"""

from __future__ import annotations

import json
import re
from typing import Any

from evalkit.config import RESULT_TEXT_TARGET
from evalkit.models import Criterion, CriterionResult, Trial


def _resolve(criterion: Criterion, trial: Trial) -> tuple[str | None, bool]:
    """Return ``(content, present)`` for the criterion's target.

    ``present`` is False when the target file was never captured or came back
    absent (None). ``__result_text__`` is always present.

    ``node:<id>`` targets the ``output`` field of the matching trajectory step,
    enabling per-node output assertions in the harness-mechanics suite.
    """
    target = criterion.target
    if target == RESULT_TEXT_TARGET:
        return trial.result_text, True
    if target is not None and target.startswith("node:"):
        try:
            nid = int(target[5:])
        except ValueError:
            return None, False
        for step in trial.trajectory:
            if step.get("node_id") == nid:
                output = step.get("output")
                if output is None:
                    return None, False
                return str(output), True
        return None, False
    if target is None:
        return None, False
    content = trial.side_effects.get(target)
    return content, content is not None


def _result(criterion: Criterion, passed: bool, evidence: str) -> CriterionResult:
    return CriterionResult(
        criterion_text=criterion.text,
        passed=passed,
        evidence=evidence,
        grader_used="deterministic",
    )


def _count_json_match(content: str, value: Any) -> tuple[bool, str]:
    try:
        parsed: Any = json.loads(content)
    except json.JSONDecodeError:
        # Fall back to the last non-empty line as JSONL.
        lines = [ln for ln in content.splitlines() if ln.strip()]
        if not lines:
            return False, "target is empty / not JSON"
        try:
            parsed = json.loads(lines[-1])
        except json.JSONDecodeError:
            return False, "target is not valid JSON or JSONL"
    if not isinstance(parsed, dict):
        return False, "parsed JSON is not an object"
    if isinstance(value, list):
        missing = [k for k in value if k not in parsed]
        return (not missing, "all keys present" if not missing else f"missing keys {missing}")
    if isinstance(value, dict):
        bad = {k: v for k, v in value.items() if parsed.get(k) != v}
        return (not bad, "all key/values match" if not bad else f"mismatched {bad}")
    return False, "json_field value must be a list of keys or a dict of key/values"


def grade(criterion: Criterion, trial: Trial) -> CriterionResult:
    check = criterion.check
    content, present = _resolve(criterion, trial)
    target = criterion.target

    if check == "file_exists":
        return _result(criterion, present, f"{target}: {'present' if present else 'absent'}")

    if check == "file_absent":
        return _result(criterion, not present, f"{target}: {'absent' if not present else 'present'}")

    if check == "contains":
        if not present or content is None:
            return _result(criterion, False, f"{target}: absent, cannot contain {criterion.value!r}")
        ok = str(criterion.value) in content
        return _result(criterion, ok, f"{'found' if ok else 'missing'} substring {criterion.value!r}")

    if check == "not_contains":
        if not present or content is None:
            return _result(criterion, True, f"{target}: absent (vacuously satisfied)")
        ok = str(criterion.value) not in content
        return _result(criterion, ok, f"{'absent' if ok else 'present'}: {criterion.value!r}")

    if check == "regex":
        if not present or content is None:
            return _result(criterion, False, f"{target}: absent, regex cannot match")
        ok = re.search(str(criterion.value), content) is not None
        return _result(criterion, ok, f"/{criterion.value}/ {'matched' if ok else 'did not match'}")

    if check == "not_regex":
        if not present or content is None:
            return _result(criterion, True, f"{target}: absent (vacuously satisfied)")
        ok = re.search(str(criterion.value), content) is None
        return _result(criterion, ok, f"/{criterion.value}/ {'absent' if ok else 'matched'}")

    if check == "json_field":
        if not present or content is None:
            return _result(criterion, False, f"{target}: absent, no JSON to inspect")
        ok, why = _count_json_match(content, criterion.value)
        return _result(criterion, ok, why)

    if check == "line_count":
        if not present or content is None:
            return _result(criterion, False, f"{target}: absent")
        n = len([ln for ln in content.splitlines() if ln.strip()])
        ok = n == int(criterion.value)
        return _result(criterion, ok, f"{n} non-empty lines (want {criterion.value})")

    if check == "file_unchanged":
        before = trial.baseline.get(target) if target is not None else None
        after = trial.side_effects.get(target) if target is not None else None
        ok = before == after
        return _result(criterion, ok, "unchanged" if ok else "content changed during run")

    return _result(criterion, False, f"unknown deterministic check: {check!r}")
