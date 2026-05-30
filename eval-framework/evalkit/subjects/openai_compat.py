"""OpenAI-compatible backend — benchmark a raw model endpoint directly.

No agent scaffolding: one ``/chat/completions`` call. Pointed at the local MLX
server (the default ``base_url``) this measures the bare model you intend to
ship — a useful baseline/control against the full dagagent pipeline. Stdlib
only: the HTTP call uses ``urllib``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from evalkit.subjects.base import RunResult

_DEFAULT_BASE_URL = "http://localhost:8080/v1"


def chat_once(
    *,
    base_url: str,
    model: str,
    prompt: str,
    api_key: str = "",
    system: str | None = None,
    timeout_s: int = 180,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One non-streaming chat completion. Returns ``(text, usage, envelope)``."""
    url = base_url.rstrip("/") + "/chat/completions"
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = json.dumps({"model": model, "messages": messages, "stream": False}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - configured base_url
        body = resp.read().decode("utf-8")

    envelope: Any = json.loads(body)
    if not isinstance(envelope, dict):
        return "", {}, {}
    text = ""
    choices = envelope.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            text = message["content"]
    usage = envelope.get("usage")
    return text, (usage if isinstance(usage, dict) else {}), envelope


class OpenAICompatSubject:
    """Subject backend: a raw OpenAI-compatible chat endpoint (e.g. MLX)."""

    name = "openai-compat"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL))
        self._api_key = str(config.get("api_key", ""))
        self._system = config.get("system")

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        try:
            text, _usage, envelope = chat_once(
                base_url=self._base_url,
                model=model,
                prompt=prompt,
                api_key=self._api_key,
                system=self._system,
                timeout_s=timeout_s,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            return RunResult(result_text="", success=False, error=f"endpoint error: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            return RunResult(result_text="", success=False, error=f"{type(exc).__name__}: {exc}")
        return RunResult(
            result_text=text,
            trajectory=[],
            cost_usd=None,
            success=bool(text),
            error=None if text else "empty completion",
            raw_envelope=envelope,
            raw_stdout=json.dumps(envelope),
        )
