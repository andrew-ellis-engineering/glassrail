"""The judge — an independent LLM used only by the ``llm`` grader.

Decoupled from the subject on purpose (principle 6): you can benchmark a local
model while judging with a stronger one, or point the judge at the same MLX
endpoint to keep everything self-hosted. A judge is just a callable that takes a
prompt and returns the model's text (or ``None`` on failure — the grader then
fails closed).
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from evalkit.subjects import claude_cli, openai_compat


class Judge(Protocol):
    def __call__(self, prompt: str, *, timeout_s: int = 120) -> str | None: ...


def build_judge(
    *, model: str, backend: str = "claude-cli", config: dict[str, Any] | None = None
) -> Judge:
    """Build a judge callable for ``backend`` (claude-cli or openai-compat)."""
    config = config or {}

    if backend in ("claude-cli", "claude"):
        def claude_judge(prompt: str, *, timeout_s: int = 120) -> str | None:
            res = claude_cli.invoke_claude(
                prompt, model=model, output_format="json", timeout_s=timeout_s
            )
            if res.returncode != 0:
                return None
            return claude_cli.extract_result_text(res.stdout)

        return claude_judge

    if backend in ("openai-compat", "mlx"):
        base_url = str(config.get("base_url", "http://localhost:8080/v1"))
        api_key = str(config.get("api_key", ""))
        api_key_env = config.get("api_key_env")
        if not api_key and isinstance(api_key_env, str):
            api_key = os.environ.get(api_key_env, "")
        if (
            not api_key
            and base_url.rstrip("/") == "https://openrouter.ai/api/v1"
        ):
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
        raw_extra = config.get("extra_body")
        extra_body = raw_extra if isinstance(raw_extra, dict) else None

        def openai_judge(prompt: str, *, timeout_s: int = 120) -> str | None:
            try:
                text, _usage, _env = openai_compat.chat_once(
                    base_url=base_url, model=model, prompt=prompt,
                    api_key=api_key, extra_body=extra_body, timeout_s=timeout_s,
                )
            except Exception:  # noqa: BLE001 - any judge failure → fail closed
                return None
            return text or None

        return openai_judge

    raise ValueError(f"unknown judge backend {backend!r}")
