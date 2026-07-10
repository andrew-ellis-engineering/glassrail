"""The Subject seam — the system under test, behind a normalized result.

A :class:`Subject` takes a prompt and returns a :class:`RunResult`: the primary
output text, a normalized trajectory, an optional cost, and the raw backend
envelope. Every grader works off the ``RunResult`` / :class:`~evalkit.models.Trial`
evidence alone, so backends are interchangeable — ``claude -p``, the Glassrail
CLI / gateway, or a raw OpenAI-compatible endpoint all look identical
downstream. This is the one place the framework knows *how* to invoke a system;
everything else is backend-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RunResult:
    """The normalized outcome of one invocation of a subject."""

    result_text: str
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float | None = None
    total_tokens: int | None = None
    success: bool = False
    error: str | None = None
    raw_envelope: dict[str, Any] = field(default_factory=dict)
    raw_stdout: str = ""
    raw_stderr: str = ""
    # True only when the invocation plumbing failed. A parseable model/agent
    # failure is gradeable evidence and leaves this False.
    infra_error: bool = False


class Subject(Protocol):
    """A system under test. Construct from a config dict, then ``run`` a prompt."""

    name: str

    def run(self, *, prompt: str, model: str, max_turns: int, timeout_s: int) -> RunResult: ...
