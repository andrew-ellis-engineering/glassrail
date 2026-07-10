"""claude -p backend — run an AI skill via the Claude Code CLI.

This is the original subject (the framework began life evaluating Claude
skills). It shells out to ``claude -p`` with a clean environment — stripping
``CLAUDE_CODE*`` and ``AI_AGENT`` env vars so a nested invocation from inside an
active Claude Code session still initializes cleanly (a documented gotcha).

``invoke_claude`` and ``extract_result_text`` are also reused by the LLM judge
(see :mod:`evalkit.judge`); the judge is a one-shot, non-agentic call.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from evalkit.subjects.base import RunResult


@dataclass
class ClaudeResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _clean_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE_CODE") and k != "AI_AGENT"
    }


def invoke_claude(
    prompt: str,
    *,
    model: str,
    output_format: str = "json",
    max_turns: int | None = None,
    permission_mode: str | None = None,
    timeout_s: int = 180,
) -> ClaudeResult:
    """Run ``claude -p`` and return its captured result."""
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", output_format]
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if permission_mode is not None:
        cmd += ["--permission-mode", permission_mode]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_clean_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ClaudeResult(
            returncode=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + "\n[timed out]",
            timed_out=True,
        )
    except FileNotFoundError:
        return ClaudeResult(
            returncode=127, stdout="", stderr="claude CLI not found on PATH", timed_out=False
        )
    return ClaudeResult(
        returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, timed_out=False
    )


def extract_result_text(stdout: str) -> str:
    """Pull the ``result`` field from a ``--output-format json`` envelope.

    Falls back to the raw stdout when it isn't a JSON envelope (e.g. text mode).
    """
    try:
        envelope: Any = json.loads(stdout)
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
            return envelope["result"]
    except json.JSONDecodeError:
        pass
    return stdout


def _extract_trajectory(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull tool_use blocks from messages[].content[], if the envelope has them."""
    out: list[dict[str, Any]] = []
    messages = envelope.get("messages")
    if not isinstance(messages, list):
        return out
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out.append({"tool": block.get("name", ""), "input": block.get("input", {})})
    return out


class ClaudeCliSubject:
    """Subject backend: an AI skill invoked through ``claude -p``."""

    name = "claude-cli"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        # Agentic runs may edit files (acceptEdits); judges/read-only runs don't.
        self._agentic = bool(config.get("agentic", True))

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        res = invoke_claude(
            prompt,
            model=model,
            output_format="json",
            max_turns=max_turns,
            permission_mode="acceptEdits" if self._agentic else None,
            timeout_s=timeout_s,
        )
        envelope: dict[str, Any] = {}
        error: str | None = "timed out" if res.timed_out else None
        if res.stdout.strip():
            try:
                parsed: Any = json.loads(res.stdout)
                if isinstance(parsed, dict):
                    envelope = parsed
            except json.JSONDecodeError as exc:
                error = error or f"could not parse envelope: {exc}"

        rt = envelope.get("result")
        result_text = rt if isinstance(rt, str) else ""
        raw_cost = envelope.get("total_cost_usd")
        cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        success = res.returncode == 0 and error is None and envelope.get("is_error") is not True
        infra_error = res.timed_out or res.returncode == 127 or bool(res.stdout.strip() and not envelope)
        return RunResult(
            result_text=result_text,
            trajectory=_extract_trajectory(envelope),
            cost_usd=cost,
            success=success,
            error=error,
            raw_envelope=envelope,
            raw_stdout=res.stdout,
            raw_stderr=res.stderr,
            infra_error=infra_error,
        )
