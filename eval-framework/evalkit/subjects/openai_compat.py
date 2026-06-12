"""OpenAI-compatible backend — benchmark a raw model endpoint directly.

No agent scaffolding: one ``/chat/completions`` call. Pointed at the local MLX
server (the default ``base_url``) this measures the bare model you intend to
ship — a useful baseline/control against the full glassrail pipeline. Stdlib
only: the HTTP call uses ``urllib``.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

from evalkit.subjects.base import RunResult

_DEFAULT_BASE_URL = "http://localhost:8080/v1"
_CAFILE_FALLBACKS = ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem")
_ERROR_BODY_LIMIT = 2000


def _ssl_context() -> ssl.SSLContext | None:
    """Return a fallback TLS context when this Python lacks a default CA file."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None
    if ssl.get_default_verify_paths().cafile:
        return None
    for cafile in _CAFILE_FALLBACKS:
        if os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return None


def _http_error_body(exc: urllib.error.HTTPError) -> str:
    body = getattr(exc, "_glassrail_error_body", "")
    if isinstance(body, str) and body:
        return body
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except OSError:
        body = ""
    if len(body) > _ERROR_BODY_LIMIT:
        body = body[:_ERROR_BODY_LIMIT] + "...<truncated>"
    error_with_body: Any = exc
    error_with_body._glassrail_error_body = body
    return body


def format_endpoint_error(exc: urllib.error.URLError | TimeoutError) -> str:
    """Return a useful, bounded error string for endpoint failures."""
    if isinstance(exc, urllib.error.HTTPError):
        body = _http_error_body(exc)
        detail = f": {body}" if body else ""
        return f"HTTP {exc.code} {exc.reason}{detail}"
    return str(exc)


def chat_completion_envelope(
    *,
    base_url: str,
    body: dict[str, Any],
    api_key: str = "",
    timeout_s: int = 180,
) -> dict[str, Any]:
    """POST a chat completion, retrying once when a provider requires reasoning."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response_body = _post_chat_completion(
            url=url,
            body=body,
            headers=headers,
            timeout_s=timeout_s,
        )
    except urllib.error.HTTPError as exc:
        error_body = _http_error_body(exc)
        retry_body = _without_disabled_reasoning(body)
        if retry_body is None or not _requires_reasoning(error_body):
            raise
        response_body = _post_chat_completion(
            url=url,
            body=retry_body,
            headers=headers,
            timeout_s=timeout_s,
        )

    envelope: Any = json.loads(response_body)
    return envelope if isinstance(envelope, dict) else {}


def _post_chat_completion(
    *,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout_s: int,
) -> str:
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s, context=_ssl_context()) as resp:
        return resp.read().decode("utf-8")


def _requires_reasoning(error_body: str) -> bool:
    lowered = error_body.lower()
    return "reasoning is mandatory" in lowered and "cannot be disabled" in lowered


def _without_disabled_reasoning(body: dict[str, Any]) -> dict[str, Any] | None:
    reasoning = body.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    effort = reasoning.get("effort")
    disabled = effort == "none" or reasoning.get("enabled") is False
    if not disabled:
        return None
    retry_body = dict(body)
    retry_body.pop("reasoning", None)
    return retry_body


def chat_once(
    *,
    base_url: str,
    model: str,
    prompt: str,
    api_key: str = "",
    system: str | None = None,
    extra_body: dict[str, Any] | None = None,
    timeout_s: int = 180,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """One non-streaming chat completion. Returns ``(text, usage, envelope)``."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if extra_body:
        body.update(extra_body)
    envelope = chat_completion_envelope(
        base_url=base_url,
        body=body,
        api_key=api_key,
        timeout_s=timeout_s,
    )
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
        api_key_env = config.get("api_key_env")
        if not self._api_key and isinstance(api_key_env, str):
            self._api_key = os.environ.get(api_key_env, "")
        if not self._api_key and self._base_url.rstrip("/") == "https://openrouter.ai/api/v1":
            self._api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self._system = config.get("system")
        raw_extra = config.get("extra_body")
        self._extra_body = raw_extra if isinstance(raw_extra, dict) else None

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult:
        try:
            text, usage, envelope = chat_once(
                base_url=self._base_url,
                model=model,
                prompt=prompt,
                api_key=self._api_key,
                system=self._system,
                extra_body=self._extra_body,
                timeout_s=timeout_s,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            return RunResult(
                result_text="",
                success=False,
                error=f"endpoint error: {format_endpoint_error(exc)}",
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return RunResult(result_text="", success=False, error=f"{type(exc).__name__}: {exc}")
        raw_tokens = usage.get("total_tokens")
        total_tokens = int(raw_tokens) if isinstance(raw_tokens, (int, float)) else None
        return RunResult(
            result_text=text,
            trajectory=[],
            cost_usd=None,
            total_tokens=total_tokens,
            success=bool(text),
            error=None if text else "empty completion",
            raw_envelope=envelope,
            raw_stdout=json.dumps(envelope),
        )
