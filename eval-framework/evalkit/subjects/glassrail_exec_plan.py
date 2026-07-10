"""glassrail-exec-plan backend — run a fixed plan through the executor.

Calls ``glassrail exec-plan <plan_path> --json`` instead of
``glassrail run <prompt> --json``.  The plan path is resolved from the
prompt string (which the runner populates with the absolute path after
resolving the ``__EXEC_PLAN__`` fixture directive).

The scripted provider path is injected into the subprocess env via
``GLASSRAIL_TIER0__SCRIPTED_PATH`` when ``scripted_responses`` is present
in the backend_config (set by the task's fixture install / backend_config).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from evalkit.subjects.base import RunResult
from evalkit.subjects.glassrail_cli import _as_text, _result_from_proc

_DEFAULT_COMMAND = ["glassrail", "exec-plan"]


class GlassrailExecPlanSubject:
    """Subject backend: the Glassrail executor driven with a fixed injected plan."""

    name = "glassrail-exec-plan"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        cmd = config.get("command", _DEFAULT_COMMAND)
        self._command = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        self._extra_args = [str(a) for a in config.get("args", [])]
        raw_env = config.get("env") or {}
        self._env_overrides = {str(k): str(v) for k, v in raw_env.items()}
        # Optional: absolute path to the scripted responses JSONL for this task.
        # The runner resolves it and passes it here via backend_config.
        self._scripted_path: str | None = config.get("scripted_responses")

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        # ``prompt`` is the absolute plan path, resolved by the runner from the
        # ``__EXEC_PLAN__ fixtures/plan.json`` directive in prompt.md.
        plan_path = prompt.strip()
        if not plan_path:
            return RunResult(
                result_text="",
                success=False,
                error="exec-plan: no plan path provided",
                infra_error=True,
            )

        cmd = [*self._command, plan_path, "--json", *self._extra_args]
        env = dict(os.environ)
        if self._env_overrides:
            env.update(self._env_overrides)
        if self._scripted_path:
            # Propagate to all four tiers so THINK/reasoning_required nodes that
            # route to tier 2 also get scripted responses, not live model calls.
            for tier in range(4):
                env[f"GLASSRAIL_TIER{tier}__SCRIPTED_PATH"] = self._scripted_path

        try:
            proc = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False, env=env
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                result_text="",
                success=False,
                error="timed out",
                raw_stdout=_as_text(exc.stdout),
                raw_stderr=_as_text(exc.stderr) + "\n[timed out]",
                infra_error=True,
            )
        except FileNotFoundError:
            return RunResult(
                result_text="",
                success=False,
                error=f"glassrail CLI not found: {self._command[0]!r}",
                infra_error=True,
            )
        return _result_from_proc(proc.returncode, proc.stdout, proc.stderr)
