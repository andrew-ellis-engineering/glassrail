"""Typed identifiers and ID generation.

Task IDs are ULIDs — 26-character Crockford-base32, lexicographically sortable
by creation time. The :class:`TaskId` ``NewType`` gives us static-typing
discipline at zero runtime cost.
"""

from __future__ import annotations

from typing import NewType

from ulid import ULID

TaskId = NewType("TaskId", str)


def new_task_id() -> TaskId:
    """Return a freshly generated, time-sortable task ID."""
    return TaskId(str(ULID()))
