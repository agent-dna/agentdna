from __future__ import annotations

import json
from typing import Any


def build_mcp_tool_call_payload(
    name: str,
    arguments: dict[str, Any] | None,
) -> str:
    """
    Build the canonical AgentDNA representation of one MCP
    tools/call request.

    Every integration that signs a tools/call - the client adapters and
    the gateway - uses this, so a call is recorded the same way whichever
    path it took.

    JSON-RPC framing is intentionally excluded.
    """

    return json.dumps(
        {
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
