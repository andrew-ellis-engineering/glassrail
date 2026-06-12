"""ReAct-style OpenAI-compatible tool loop baseline.

This subject is intentionally conventional: the model gets a single local
``file_read`` tool and loops until it returns final assistant text or exhausts
``max_turns``. It reaches the model over an OpenAI-compatible HTTP boundary and
executes tools inside the eval harness, never by importing Glassrail.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from evalkit.subjects.base import RunResult
from evalkit.subjects.openai_compat import _ssl_context

_DEFAULT_BASE_URL = "http://localhost:8080/v1"
_DEFAULT_SYSTEM = (
    "Answer the user's task. A file_read tool is available; call it when you "
    "need to inspect a file path. When you have enough information, give the "
    "final answer as plain text."
)
_FILE_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "file_read",
        "description": "Read a UTF-8 text file from the local eval fixture filesystem.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read."}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


class ReactLoopSubject:
    """Subject backend: a minimal ReAct loop with a local file_read tool."""

    name = "react-loop"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL))
        self._api_key = str(config.get("api_key", ""))
        api_key_env = config.get("api_key_env")
        if not self._api_key and isinstance(api_key_env, str):
            self._api_key = os.environ.get(api_key_env, "")
        if (
            not self._api_key
            and self._base_url.rstrip("/") == "https://openrouter.ai/api/v1"
        ):
            self._api_key = os.environ.get("OPENROUTER_API_KEY", "")
        raw_extra = config.get("extra_body")
        self._extra_body = raw_extra if isinstance(raw_extra, dict) else None
        self._system = str(config.get("system") or _DEFAULT_SYSTEM)

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": prompt},
        ]
        trajectory: list[dict[str, Any]] = []
        total_tokens = 0
        envelopes: list[dict[str, Any]] = []

        try:
            for _turn in range(max_turns):
                envelope = self._chat(
                    model=model,
                    messages=messages,
                    timeout_s=timeout_s,
                )
                envelopes.append(envelope)
                raw_tokens = (envelope.get("usage") or {}).get("total_tokens")
                if isinstance(raw_tokens, (int, float)):
                    total_tokens += int(raw_tokens)

                message = _first_message(envelope)
                if message is None:
                    return _failed(
                        "missing assistant message",
                        envelopes,
                        trajectory,
                        total_tokens,
                    )

                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    messages.append(message)
                    for call in tool_calls:
                        tool_result, step = _execute_tool_call(call)
                        trajectory.append(step)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(call.get("id") or ""),
                                "content": tool_result,
                            }
                        )
                    continue

                content = message.get("content")
                text = content if isinstance(content, str) else ""
                return RunResult(
                    result_text=text,
                    trajectory=trajectory,
                    cost_usd=None,
                    total_tokens=total_tokens,
                    success=bool(text),
                    error=None if text else "empty completion",
                    raw_envelope={"turns": envelopes},
                    raw_stdout=json.dumps({"turns": envelopes}),
                )
        except (urllib.error.URLError, TimeoutError) as exc:
            return RunResult(result_text="", success=False, error=f"endpoint error: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            return RunResult(result_text="", success=False, error=f"{type(exc).__name__}: {exc}")

        return _failed("max turns exhausted", envelopes, trajectory, total_tokens)

    def _chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_s: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": [_FILE_READ_TOOL],
            "stream": False,
        }
        if self._extra_body:
            body.update(self._extra_body)
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(
            self._base_url.rstrip("/") + "/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(  # noqa: S310 - configured base_url
            req, timeout=timeout_s, context=_ssl_context()
        ) as resp:
            parsed: Any = json.loads(resp.read().decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}


def _first_message(envelope: dict[str, Any]) -> dict[str, Any] | None:
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    return message if isinstance(message, dict) else None


def _execute_tool_call(call: Any) -> tuple[str, dict[str, Any]]:
    function = call.get("function") if isinstance(call, dict) else None
    function = function if isinstance(function, dict) else {}
    name = str(function.get("name") or "")
    args = _parse_args(function.get("arguments"))
    step = {"tool": name or "tool", "input": args}
    if name != "file_read":
        return f"ERROR: unknown tool {name!r}", step

    raw_path = args.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return "ERROR: file_read requires a string path", step
    try:
        return Path(raw_path).expanduser().read_text(encoding="utf-8"), step
    except OSError as exc:
        return f"ERROR: {exc}", step


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _failed(
    error: str,
    envelopes: list[dict[str, Any]],
    trajectory: list[dict[str, Any]],
    total_tokens: int,
) -> RunResult:
    return RunResult(
        result_text="",
        trajectory=trajectory,
        cost_usd=None,
        total_tokens=total_tokens or None,
        success=False,
        error=error,
        raw_envelope={"turns": envelopes},
        raw_stdout=json.dumps({"turns": envelopes}),
    )
