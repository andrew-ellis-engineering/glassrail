"""glassrail-cli backend — run Glassrail end to end as a subprocess.

Mirrors ``claude -p`` exactly: shell out to ``glassrail run <prompt> --json`` and
parse a JSON envelope. Glassrail routes through its *own* tier config, so
this benchmarks the model(s) you actually ship — tier 0 is your local MLX
server by default. ``--model`` overrides tier 0's model for the run; the real
control over which models run is Glassrail's own settings (config.toml / env).

The envelope is produced by the Glassrail CLI and already carries a normalized
``trajectory`` (node sequence, tiers, branch decisions), ``result`` (the task's
final_output), ``total_cost_usd``, and ``is_error`` — so there is no
glassrail-specific parsing here beyond reading those fields.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from evalkit.subjects.base import RunResult

_DEFAULT_COMMAND = ["glassrail", "run"]


def _as_text(stream: str | bytes | None) -> str:
    """Coerce a captured stream to str (TimeoutExpired may yield bytes)."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


class GlassrailCliSubject:
    """Subject backend: Glassrail driven through its headless CLI."""

    name = "glassrail-cli"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        cmd = config.get("command", _DEFAULT_COMMAND)
        self._command = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        self._extra_args = [str(a) for a in config.get("args", [])]
        # Optional env overrides layered onto the inherited environment, so the
        # suite can pin the agent's tier config (e.g. a longer tier-0 timeout)
        # without depending on the caller's shell.
        raw_env = config.get("env") or {}
        self._env_overrides = {str(k): str(v) for k, v in raw_env.items()}

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        cmd = [*self._command, prompt, "--json"]
        if model:
            cmd += ["--model", model]
        if timeout_s:
            cmd += ["--timeout", str(timeout_s)]
        cmd += self._extra_args
        env = {**os.environ, **self._env_overrides} if self._env_overrides else None
        try:
            proc = subprocess.run(  # noqa: S603 - argv from config, no shell
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False, env=env
            )
        except subprocess.TimeoutExpired as exc:
            # On timeout, CPython may hand back stdout/stderr as bytes even
            # though text=True was requested — coerce before touching them, or
            # `str + bytes` raises TypeError and masks the real timeout.
            return RunResult(
                result_text="",
                success=False,
                error="timed out",
                raw_stdout=_as_text(exc.stdout),
                raw_stderr=_as_text(exc.stderr) + "\n[timed out]",
            )
        except FileNotFoundError:
            return RunResult(
                result_text="",
                success=False,
                error=f"glassrail CLI not found: {self._command[0]!r}",
            )
        return _result_from_proc(proc.returncode, proc.stdout, proc.stderr)


def _result_from_proc(returncode: int, stdout: str, stderr: str) -> RunResult:
    envelope = _parse_envelope(stdout)
    error: str | None = None
    if envelope is None:
        envelope = {}
        error = "could not parse glassrail envelope"

    rt = envelope.get("result")
    result_text = rt if isinstance(rt, str) else ""
    # When the planner rejects a task it sets status="rejected" and puts its
    # reasoning in the error field while result stays empty.  Surface that
    # reasoning as result_text so graders can evaluate the rejection message
    # the same way they would a normal result.
    if not result_text and envelope.get("status") == "rejected":
        result_text = str(envelope.get("error") or "")
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
