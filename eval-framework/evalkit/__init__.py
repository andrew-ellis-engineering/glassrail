"""evalkit — a multi-trial evaluation framework for AI skills.

Runs each task k times via ``claude -p``, captures three evidence channels
(output, side-effects, tool trajectory), grades with a
deterministic → trajectory → LLM cascade, and reports pass@k vs pass^k.
"""

from __future__ import annotations

from evalkit.config import HARNESS_VERSION

__version__ = HARNESS_VERSION

__all__ = ["__version__"]
