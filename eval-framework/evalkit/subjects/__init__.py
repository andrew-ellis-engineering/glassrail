"""Subjects — the systems a suite can be run against.

A suite or task names a ``backend`` (with optional ``backend_config``); the
runner calls :func:`build_subject` to construct it. Backends are
interchangeable because every one returns a normalized
:class:`~evalkit.subjects.base.RunResult`.
"""

from __future__ import annotations

from typing import Any

from evalkit.subjects.base import RunResult, Subject
from evalkit.subjects.claude_cli import ClaudeCliSubject
from evalkit.subjects.glassrail_cli import GlassrailCliSubject
from evalkit.subjects.glassrail_exec_plan import GlassrailExecPlanSubject
from evalkit.subjects.glassrail_gateway import GlassrailGatewaySubject
from evalkit.subjects.openai_compat import OpenAICompatSubject
from evalkit.subjects.react_loop import ReactLoopSubject

_REGISTRY: dict[str, type] = {
    "claude-cli": ClaudeCliSubject,
    "glassrail-cli": GlassrailCliSubject,
    "glassrail-exec-plan": GlassrailExecPlanSubject,
    "glassrail-gateway": GlassrailGatewaySubject,
    "openai-compat": OpenAICompatSubject,
    "react-loop": ReactLoopSubject,
}


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def build_subject(backend: str, config: dict[str, Any] | None = None) -> Subject:
    """Construct a subject for ``backend`` from its config dict."""
    try:
        cls = _REGISTRY[backend]
    except KeyError:
        raise ValueError(
            f"unknown backend {backend!r}; choose from {available_backends()}"
        ) from None
    return cls(config or {})


__all__ = [
    "ClaudeCliSubject",
    "GlassrailCliSubject",
    "GlassrailExecPlanSubject",
    "GlassrailGatewaySubject",
    "OpenAICompatSubject",
    "ReactLoopSubject",
    "RunResult",
    "Subject",
    "available_backends",
    "build_subject",
]
