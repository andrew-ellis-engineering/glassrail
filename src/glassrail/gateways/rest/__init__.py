"""FastAPI gateway.

Endpoints: ``/task``, ``/task/{id}``, ``/task/{id}/resume``,
``/task/{id}/branch-log``, ``/tools``, ``/health``. Phase 1 adds the
SSE/WebSocket stream over the event bus.

The ASGI app is exposed as ``app`` for uvicorn:

    uvicorn glassrail.gateways.rest:app
"""

from __future__ import annotations

from glassrail.gateways.rest.app import app, create_app, create_default_app

__all__ = ["app", "create_app", "create_default_app"]
