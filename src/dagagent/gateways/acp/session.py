"""In-memory ACP session registry.

A session is a conversation thread: an ordered list of the tasks run under it
and the carried context that lets a follow-up prompt build on the previous
result (the *dovetail* in the spec). v1 keeps sessions only for the life of the
adapter process — no persistence, no ``session/load``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from dagagent.core import TaskId


@dataclass
class Session:
    """One ACP session: its task history and the context carried forward."""

    id: str
    tasks: list[TaskId] = field(default_factory=list)
    # The prior task's final_output, threaded into the next prompt (dovetail).
    carried_context: str | None = None
    # Set while a turn is running so session/cancel can target the live task.
    active_task: TaskId | None = None

    def compose_request(self, text: str) -> str:
        """Build a follow-up task's request, threading the prior result in.

        The first prompt in a session is used verbatim. Later prompts are
        prefixed with the previous task's ``final_output`` as a context preamble
        so the task *dovetails* — it sees what came before. This is task input,
        not node context, so the fresh-context-per-node invariant is untouched.
        """
        if not self.carried_context:
            return text
        return f"Context from the previous step:\n{self.carried_context}\n\nNew request: {text}"


class SessionRegistry:
    """Keyed by ACP ``sessionId``; lives for the adapter process."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        session_id = f"sess_{uuid.uuid4().hex}"
        session = Session(id=session_id)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)
