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
from evalkit.subjects.dagagent_cli import DagAgentCliSubject
from evalkit.subjects.dagagent_exec_plan import DagAgentExecPlanSubject
from evalkit.subjects.dagagent_gateway import DagAgentGatewaySubject
from evalkit.subjects.openai_compat import OpenAICompatSubject

_REGISTRY: dict[str, type] = {
    "claude-cli": ClaudeCliSubject,
    "dagagent-cli": DagAgentCliSubject,
    "dagagent-exec-plan": DagAgentExecPlanSubject,
    "dagagent-gateway": DagAgentGatewaySubject,
    "openai-compat": OpenAICompatSubject,
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
    "DagAgentCliSubject",
    "DagAgentExecPlanSubject",
    "DagAgentGatewaySubject",
    "OpenAICompatSubject",
    "RunResult",
    "Subject",
    "available_backends",
    "build_subject",
]
