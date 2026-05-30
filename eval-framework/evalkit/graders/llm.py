"""LLM judge — semantic judgment, last resort (principles 1, 5, 6).

One independent judge call per criterion, reference-guided by the task's
``expected_behavior`` (which is NEVER shown to the agent under test). The judge
evaluates exactly one dimension and returns ``PASS``/``FAIL``/``UNKNOWN`` on
the first line. Fail-closed: anything that isn't a clear PASS does not pass.

The judge itself is a backend-agnostic callable (see :mod:`evalkit.judge`) —
it can be Claude, a local MLX model, or anything OpenAI-compatible.
"""

from __future__ import annotations

from evalkit.judge import Judge
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


def grade(
    criterion: Criterion,
    trial: Trial,
    *,
    expected_behavior: str,
    judge: Judge,
    timeout_s: int = 120,
) -> CriterionResult:
    prompt = _JUDGE_TEMPLATE.format(
        expected=expected_behavior.strip() or "(none provided)",
        criterion=criterion.text,
        output=trial.result_text.strip() or "(empty output)",
        side_effects=_side_effects_block(trial),
    )
    verdict_text = judge(prompt, timeout_s=timeout_s)
    if verdict_text is None:
        return _result(criterion, False, "judge invocation failed")

    verdict_text = verdict_text.strip()
    lines = verdict_text.splitlines()
    first_line = lines[0].strip().upper() if lines else ""
    rationale = lines[1].strip() if len(lines) > 1 else ""

    if first_line.startswith("PASS"):
        return _result(criterion, True, rationale or "judge: PASS")
    if first_line.startswith("FAIL"):
        return _result(criterion, False, rationale or "judge: FAIL")
    return _result(criterion, False, f"judge inconclusive ({first_line or 'empty'}): {rationale}")
