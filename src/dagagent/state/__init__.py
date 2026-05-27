"""Persistence layer.

- :mod:`dagagent.state.base` defines the :class:`StateStore` Protocol.
- :mod:`dagagent.state.memory` provides the in-process implementation used
  for tests and ephemeral runs.
- A SQLite-backed implementation will land as :mod:`dagagent.state.sqlite`.
"""

from __future__ import annotations

from dagagent.state.base import StateStore
from dagagent.state.memory import InMemoryStateStore

__all__ = ["InMemoryStateStore", "StateStore"]
