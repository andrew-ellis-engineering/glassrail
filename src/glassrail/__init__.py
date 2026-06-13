"""glassrail — a DAG-planning agent.

See the docs site for design and architecture.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("glassrail")
except PackageNotFoundError:  # pragma: no cover - only when imported unpackaged
    __version__ = "0.0.0"

__all__ = ["__version__"]
