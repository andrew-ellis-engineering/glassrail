"""glassrail-gateway backend — drive a running REST gateway over HTTP.

POST ``/task``, poll ``GET /task/{id}`` until the task reaches a terminal state,
then read ``final_output`` and synthesize a normalized trajectory from the
recorded ExecutionState. Exercises the real deployment surface (the FastAPI
gateway + the agent's tier routing). Stdlib only: HTTP via ``urllib``.

The trajectory token convention here MUST match the Glassrail CLI's so a suite
grades identically whichever glassrail backend it runs against: tool nodes → the
tool name; every other node (decision / think / summary / synthesis / result /
subplan) → its node type. The branch a decision took lives in ``branch_taken``,
not in the token.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from evalkit.subjects.base import RunResult

_DEFAULT_BASE_URL = "http://localhost:8000"
_TERMINAL = {"completed", "failed", "awaiting_confirmation"}


class GlassrailGatewaySubject:
    """Subject backend: Glassrail driven through a running REST gateway."""

    name = "glassrail-gateway"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")
        self._poll_interval = float(config.get("poll_interval_s", 0.5))

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        try:
            submitted = self._post("/task", {"request": prompt}, timeout_s)
        except (urllib.error.URLError, TimeoutError) as exc:
            return RunResult(result_text="", success=False, error=f"gateway unreachable: {exc}")
        task_id = submitted.get("task_id")
        if not task_id:
            return RunResult(
                result_text="", success=False, error="gateway returned no task_id",
                raw_envelope=submitted,
            )

        deadline = time.monotonic() + timeout_s
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                state = self._get(f"/task/{task_id}", timeout_s)
            except (urllib.error.URLError, TimeoutError) as exc:
                return RunResult(result_text="", success=False, error=f"gateway poll failed: {exc}")
            if state.get("status") in _TERMINAL:
                break
            time.sleep(self._poll_interval)
        else:
            return RunResult(result_text="", success=False, error="timed out", raw_envelope=state)
        return result_from_state(state)

    def _post(self, path: str, body: dict[str, Any], timeout_s: int) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._base_url + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        return self._read(req, timeout_s)

    def _get(self, path: str, timeout_s: int) -> dict[str, Any]:
        req = urllib.request.Request(self._base_url + path, method="GET")
        return self._read(req, timeout_s)

    @staticmethod
    def _read(req: urllib.request.Request, timeout_s: int) -> dict[str, Any]:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - configured base_url
            parsed: Any = json.loads(resp.read().decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}


def _node_token(node: dict[str, Any]) -> str:
    node_type = str(node.get("type", "node"))
    if node_type == "tool":
        return str(node.get("tool") or "tool")
    return node_type


def trajectory_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Map an ExecutionState JSON dump into normalized trajectory steps."""
    plan = state.get("plan") or {}
    nodes_by_id: dict[str, dict[str, Any]] = {
        str(n.get("id")): n for n in plan.get("nodes", []) if isinstance(n, dict)
    }
    results = state.get("results") or {}
    order = plan.get("sorted_node_ids") or [n.get("id") for n in plan.get("nodes", [])]

    steps: list[dict[str, Any]] = []
    for raw_id in order:
        key = str(raw_id)
        node = nodes_by_id.get(key)
        if node is None:
            continue
        result = results.get(key) if isinstance(results, dict) else None
        result = result if isinstance(result, dict) else {}
        branch_taken = result.get("branch_taken")
        steps.append(
            {
                "tool": _node_token(node),
                "input": node.get("args_template") or {},
                "node_id": node.get("id"),
                "node_type": node.get("type"),
                "tier_used": result.get("tier_used"),
                "status": result.get("status"),
                "confidence": result.get("confidence", 1.0),
                "flagged": result.get("flagged", False),
                "branch_taken": branch_taken,
            }
        )
    return steps


def result_from_state(state: dict[str, Any]) -> RunResult:
    """Build a :class:`RunResult` from an ExecutionState JSON dump."""
    status = state.get("status")
    final_output = state.get("final_output")
    result_text = final_output if isinstance(final_output, str) else ""
    error = state.get("error") if status == "failed" else None
    if status == "failed" and not error:
        error = "task failed"
    tokens = sum(
        int(r.get("tokens_used", 0))
        for r in (state.get("results") or {}).values()
        if isinstance(r, dict)
    )
    return RunResult(
        result_text=result_text,
        trajectory=trajectory_from_state(state),
        cost_usd=None,  # local/self-hosted: token count lives in the envelope
        success=status == "completed" and error is None,
        error=error,
        raw_envelope={**state, "total_tokens": tokens},
        raw_stdout=json.dumps(state),
    )
