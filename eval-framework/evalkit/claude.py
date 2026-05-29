"""Invoke the ``claude -p`` CLI with a clean environment.

Shared by the runner (task execution) and the LLM grader (judging). Strips
``CLAUDE_CODE*`` and ``AI_AGENT`` env vars so a nested invocation from inside
an active Claude Code session still initializes cleanly (a documented gotcha).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


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
