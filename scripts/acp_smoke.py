#!/usr/bin/env python3
"""Manual smoke test for the `dagagent acp` adapter.

Spawns the adapter as a subprocess and drives a real session over stdio JSON-RPC:
initialize → session/new → session/prompt. Prints each session/update as it
streams in, then the final stopReason. Requires tier 0 (the MLX server) to be up.

    uv run python scripts/acp_smoke.py "what is 2 + 2?"
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    line = await reader.readline()
    if not line:
        return None
    return json.loads(line)


async def main(prompt: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "dagagent",
        "acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def send(obj: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        await proc.stdin.drain()

    async def await_response(expect_id: int) -> dict[str, Any]:
        """Read until the response with the given id, printing notifications.

        Auto-approves any plan-permission request so the run proceeds unattended.
        """
        while True:
            msg = await _read_message(proc.stdout)
            if msg is None:
                raise RuntimeError("adapter closed stdout unexpectedly")
            if msg.get("id") == expect_id and "method" not in msg:
                return msg
            if msg.get("method") == "session/request_permission" and "id" in msg:
                print("  [gate] auto-approving plan")
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": "approve"}},
                    }
                )
                continue
            if msg.get("method") == "session/update":
                update = msg["params"]["update"]
                kind = update.get("sessionUpdate")
                if kind == "plan":
                    statuses = " ".join(e["status"][0] for e in update["entries"])
                    print(f"  [plan] {statuses}")
                elif kind == "agent_message_chunk":
                    print(f"  [msg]  {update['content']['text'][:200]}")
                else:
                    print(f"  [{kind}] {json.dumps(update)[:160]}")

    await send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    init = await await_response(1)
    print(f"initialize → protocol v{init['result']['protocolVersion']}")

    await send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}})
    sid = (await await_response(2))["result"]["sessionId"]
    print(f"session/new → {sid}")

    print(f"session/prompt → {prompt!r}")
    await send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
        }
    )
    resp = await await_response(3)
    print(f"stopReason → {resp['result']['stopReason']}")

    proc.stdin.close()
    await proc.wait()
    return 0


if __name__ == "__main__":
    request = sys.argv[1] if len(sys.argv) > 1 else "what is 2 + 2?"
    raise SystemExit(asyncio.run(main(request)))
