"""evalkit — a multi-trial evaluation framework for agent workflows.

Runs each task k times against a pluggable *subject* (``claude -p``, the
glassrail CLI / gateway, or a raw OpenAI-compatible endpoint), captures three
evidence channels (output, side-effects, trajectory), grades with a
deterministic → trajectory → LLM cascade, and reports pass@k vs pass^k.
"""

from __future__ import annotations

from evalkit.config import HARNESS_VERSION

__version__ = HARNESS_VERSION

__all__ = ["__version__"]
