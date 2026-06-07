"""JSON-RPC 2.0 framing over stdio for the ACP adapter.

ACP speaks JSON-RPC 2.0 as newline-delimited JSON on stdin/stdout: one
message object per line. stdout is reserved for protocol traffic; everything
else (logs, diagnostics) must go to stderr — the same discipline as
``glassrail run --json``.

This module owns only the wire: reading and parsing incoming messages, and
writing responses, notifications, and outbound (agent→client) requests. The
dispatch and ACP semantics live in :mod:`server`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from typing import Any, cast

# JSON-RPC 2.0 error codes we use.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """An error to return to the peer as a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


async def stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap the process's stdin/stdout as asyncio byte streams."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, proto = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        sys.stdout.buffer,
    )
    writer = asyncio.StreamWriter(transport, proto, reader, loop)
    return reader, writer


class Connection:
    """A JSON-RPC 2.0 connection over a pair of byte streams.

    Reads are single-consumer (the server's main loop iterates :meth:`incoming`).
    Writes are serialised behind a lock so concurrently-running turn updates and
    request responses never interleave on the wire. Outbound requests (used by
    the plan-permission gate) get a monotonic id and resolve via a future when
    the matching response arrives.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}

    async def incoming(self) -> AsyncIterator[dict[str, Any]]:
        """Yield each parsed inbound message until stdin closes.

        Responses to our own outbound requests are routed to their waiting
        future here and are *not* yielded; only peer-initiated requests and
        notifications surface to the caller. Unparseable lines are answered
        with a parse-error response and skipped.
        """
        while True:
            line = await self._reader.readline()
            if not line:
                return
            text = line.strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                await self._send(
                    {"jsonrpc": "2.0", "id": None, "error": _err(PARSE_ERROR, "parse error")}
                )
                continue
            if not isinstance(msg, dict):
                continue
            message = cast("dict[str, Any]", msg)
            if self._route_response(message):
                continue
            yield message

    def _route_response(self, msg: dict[str, Any]) -> bool:
        """If ``msg`` is a response to one of our outbound requests, resolve it."""
        if "method" in msg or "id" not in msg:
            return False
        msg_id = msg.get("id")
        if not isinstance(msg_id, int):
            return False
        fut = self._pending.pop(msg_id, None)
        if fut is None:
            return False
        if not fut.done():
            if "error" in msg:
                err: dict[str, Any] = msg["error"] or {}
                fut.set_exception(
                    JsonRpcError(err.get("code", INTERNAL_ERROR), err.get("message", "error"))
                )
            else:
                fut.set_result(msg.get("result"))
        return True

    async def respond(self, request_id: Any, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(
        self, request_id: Any, code: int, message: str, data: Any | None = None
    ) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "error": _err(code, message, data)})

    async def notify(self, method: str, params: Any) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: Any) -> Any:
        """Send an agent→client request and await its response."""
        self._next_id += 1
        request_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await fut

    async def _send(self, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()


def _err(code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err
