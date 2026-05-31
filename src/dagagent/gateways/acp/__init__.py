"""ACP (Agent Client Protocol) adapter.

Exposes the agent over JSON-RPC 2.0 on stdio so a separate client — the Rust
``clients/tui`` binary, or any ACP client — can drive it as a subprocess. The
agent core does not move; this is a protocol seam over the existing
:func:`~dagagent.runtime.build_runtime`.

``run_acp`` is the entry point behind the ``dagagent acp`` CLI command.
"""

from __future__ import annotations

import logging
import sys

from dagagent.config import Settings, get_settings
from dagagent.gateways.acp.protocol import Connection, stdio_streams
from dagagent.gateways.acp.server import AcpServer
from dagagent.runtime import build_runtime

__all__ = ["run_acp"]


def _settings_for_acp() -> Settings:
    # M0 runs straight through; the plan-approval gate (request_permission)
    # arrives in M1, at which point this flips to confirm_plans=True.
    settings = get_settings()
    return settings.model_copy(update={"confirm_plans": False})


async def run_acp() -> None:
    """Serve ACP over stdio until stdin closes.

    stdout is reserved for the protocol, so all logging is forced to stderr.
    """
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    runtime = build_runtime(_settings_for_acp())
    reader, writer = await stdio_streams()
    server = AcpServer(runtime, Connection(reader, writer))
    await server.serve()
