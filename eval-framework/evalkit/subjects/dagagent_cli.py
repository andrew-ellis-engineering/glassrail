"""dagagent-cli backend — run the dagagent end to end as a subprocess.

Mirrors ``claude -p`` exactly: shell out to ``dagagent run <prompt> --json`` and
parse a JSON envelope. The dagagent routes through its *own* tier config, so
this benchmarks the model(s) you actually ship — tier 0 is your local MLX
server by default. ``--model`` overrides tier 0's model for the run; the real
control over which models run is the dagagent's own settings (config.toml / env).

The envelope is produced by the dagagent CLI and already carries a normalized
``trajectory`` (node sequence, tiers, branch decisions), ``result`` (the task's
final_output), ``total_cost_usd``, and ``is_error`` — so there is no
dagagent-specific parsing here beyond reading those fields.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from evalkit.subjects.base import RunResult

_DEFAULT_COMMAND = ["dagagent", "run"]


class DagAgentCliSubject:
    """Subject backend: the dagagent driven through its headless CLI."""

    name = "dagagent-cli"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        cmd = config.get("command", _DEFAULT_COMMAND)
        self._command = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        self._extra_args = [str(a) for a in config.get("args", [])]

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        cmd = [*self._command, prompt, "--json"]
        if model:
            cmd += ["--model", model]
        if timeout_s:
            cmd += ["--timeout", str(timeout_s)]
        cmd += self._extra_args
        try:
            proc = subprocess.run(  # noqa: S603 - argv from config, no shell
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                result_text="",
                success=False,
                error="timed out",
                raw_stdout=exc.stdout or "",
                raw_stderr=(exc.stderr or "") + "\n[timed out]",
            )
        except FileNotFoundError:
            return RunResult(
                result_text="",
                success=False,
                error=f"dagagent CLI not found: {self._command[0]!r}",
            )
        return _result_from_proc(proc.returncode, proc.stdout, proc.stderr)


def _result_from_proc(returncode: int, stdout: str, stderr: str) -> RunResult:
    envelope = _parse_envelope(stdout)
    error: str | None = None
    if envelope is None:
        envelope = {}
        error = "could not parse dagagent envelope"

    rt = envelope.get("result")
    result_text = rt if isinstance(rt, str) else ""
    trajectory = envelope.get("trajectory")
    if not isinstance(trajectory, list):
        trajectory = []
    raw_cost = envelope.get("total_cost_usd")
    cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    is_error = envelope.get("is_error") is True
    if is_error and error is None:
        error = str(envelope.get("error") or "task failed")
    success = returncode == 0 and not is_error and error is None
    return RunResult(
        result_text=result_text,
        trajectory=trajectory,
        cost_usd=cost,
        success=success,
        error=error,
        raw_envelope=envelope,
        raw_stdout=stdout,
        raw_stderr=stderr,
    )


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Parse stdout as JSON, tolerating leading log lines (use the last object)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed: Any = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None
