"""FastAPI gateway.

Endpoints: ``/task``, ``/task/{id}``, ``/task/{id}/resume``,
``/task/{id}/branch-log``, ``/tools``, ``/health``. Phase 1 adds the
SSE/WebSocket stream over the event bus.

The ASGI app is exposed as ``app`` for uvicorn:

    uvicorn dagagent.gateways.rest:app
"""
