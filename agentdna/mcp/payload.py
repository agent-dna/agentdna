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

    The relay signs exactly this, so a tools/call is always recorded in
    one canonical form, independent of the MCP client that made it.
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
