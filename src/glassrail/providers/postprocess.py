"""Post-processing helpers for raw LLM output.

Local models (Qwen3, Llama, etc.) regularly wrap their JSON responses in
markdown code fences or reasoning blocks even when ``response_format:
json_object`` is requested.  These helpers normalise that output so callers
can do a clean ``json.loads`` without per-call defensive code.
"""

from __future__ import annotations

import re

# Matches a ```json ... ``` or ``` ... ``` fence that wraps the entire output.
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def strip_model_output(raw: str) -> str:
    """Strip non-JSON wrappers that local models emit around JSON responses.

    Handles two patterns:
    - ``<think>...</think>`` blocks from Qwen3 reasoning models when
      ``/no_think`` is not set in the system prompt.
    - ``\\`\\`\\`json ... \\`\\`\\``` or ``\\`\\`\\` ... \\`\\`\\``` markdown
      code fences that Qwen3 and other local models emit even when instructed
      not to.
    """
    text = raw.strip()
    # Remove thinking blocks before extracting JSON.
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
    # Unwrap a single top-level code fence if present.
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    return text
