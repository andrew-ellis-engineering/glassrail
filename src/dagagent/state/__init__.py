"""Persistence layer.

- :mod:`dagagent.state.base` defines the :class:`StateStore` Protocol.
- :mod:`dagagent.state.memory` provides the in-process implementation used
  for tests and ephemeral runs.
- :mod:`dagagent.state.sqlite` provides the durable SQLite backend; import
  it directly (it requires the optional ``sqlite`` extra and is not
  re-exported here to keep that dependency optional).
"""

from __future__ import annotations

from dagagent.state.base import StateStore
from dagagent.state.memory import InMemoryStateStore

__all__ = ["InMemoryStateStore", "StateStore"]
