"""Persistence layer.

- :mod:`glassrail.state.base` defines the :class:`StateStore` Protocol.
- :mod:`glassrail.state.memory` provides the in-process implementation used
  for tests and ephemeral runs.
- :mod:`glassrail.state.sqlite` provides the durable SQLite backend; import
  it directly (it requires the optional ``sqlite`` extra and is not
  re-exported here to keep that dependency optional).
"""

from __future__ import annotations

from glassrail.state.base import StateStore
from glassrail.state.memory import InMemoryStateStore

__all__ = ["InMemoryStateStore", "StateStore"]
