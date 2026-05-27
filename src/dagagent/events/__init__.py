"""Typed event types and the in-process event bus.

Every plan, node, branch, tool, and flag transition emits a typed Pydantic
event onto the bus. Gateways (SSE, WebSocket, TUI) subscribe via an
``AsyncIterator``. The in-process bus is intended to be swappable for
Redis/NATS later without changing producers.
"""
