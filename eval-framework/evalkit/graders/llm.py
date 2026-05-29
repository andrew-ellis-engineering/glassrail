"""LLM judge — semantic judgment, last resort (principles 1, 5, 6).

One independent judge call per criterion, reference-guided by the task's
``expected_behavior`` (which is NEVER shown to the agent under test). The judge
evaluates exactly one dimension and returns ``PASS``/``FAIL``/``UNKNOWN`` on
the first line. Fail-closed: anything that isn't a clear PASS does not pass.
"""

from __future__ import annotations

import json
from typing import Any

from evalkit.claude import invoke_claude
from evalkit.models import Criterion, CriterionResult, Trial

_JUDGE_TEMPLATE = """\
You are grading ONE criterion of an AI task. Evaluate ONLY this criterion — \
ignore everything else about quality.

Reference (expected behavior, for your judgment only):
{expected}

Criterion to evaluate:
{criterion}

The agent's output:
---
{output}
---
{side_effects}
First, briefly decide for yourself what a correct response looks like. Then \
judge the output against THIS criterion only.

Respond with PASS, FAIL, or UNKNOWN on the first line (UNKNOWN only if the \
evidence is genuinely insufficient). Optionally one sentence of rationale on \
the second line."""


def _result(criterion: Criterion, passed: bool, evidence: str) -> CriterionResult:
    return CriterionResult(
        criterion_text=criterion.text, passed=passed, evidence=evidence, grader_used="llm"
    )


def _side_effects_block(trial: Trial) -> str:
    captured = {k: v for k, v in trial.side_effects.items() if v is not None}
    if not captured:
        return ""
    lines = ["Relevant captured side-effects:"]
    for path, content in captured.items():
        snippet = content if len(content) <= 2000 else content[:2000] + "…(truncated)"
        lines.append(f"\n[{path}]\n{snippet}")
    return "\n".join(lines) + "\n"


def _extract_result_text(stdout: str) -> str:
    try:
        envelope: Any = json.loads(stdout)
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            return envelope["result"]
    except json.JSONDecodeError:
        pass
    return stdout


def grade(
    criterion: Criterion,
    trial: Trial,
    *,
    expected_behavior: str,
    grader_model: str,
    timeout_s: int = 120,
) -> CriterionResult:
    prompt = _JUDGE_TEMPLATE.format(
        expected=expected_behavior.strip() or "(none provided)",
        criterion=criterion.text,
        output=trial.result_text.strip() or "(empty output)",
        side_effects=_side_effects_block(trial),
    )
    res = invoke_claude(prompt, model=grader_model, output_format="json", timeout_s=timeout_s)
    if res.returncode != 0:
        return _result(criterion, False, f"judge invocation failed (rc={res.returncode}): {res.stderr.strip()[:200]}")

    verdict_text = _extract_result_text(res.stdout).strip()
    first_line = verdict_text.splitlines()[0].strip().upper() if verdict_text else ""
    rationale = verdict_text.splitlines()[1].strip() if len(verdict_text.splitlines()) > 1 else ""

    if first_line.startswith("PASS"):
        return _result(criterion, True, rationale or "judge: PASS")
    if first_line.startswith("FAIL"):
        return _result(criterion, False, rationale or "judge: FAIL")
    return _result(criterion, False, f"judge inconclusive ({first_line or 'empty'}): {rationale}")
