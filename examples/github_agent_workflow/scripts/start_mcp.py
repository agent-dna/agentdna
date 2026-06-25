"""
Start the GitHub MCP server standalone.

Usage:
    python scripts/start_mcp.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.integrations.agentdna import warmup_worker
from app.mcp_server.github_mcp import mcp


if __name__ == "__main__":
    # Eager AgentDNA init for the Worker identity — the MCP server only needs
    # the Worker's DID for cbac.authorize_action(). No-op when disabled.
    warmup_worker()

    cbac_timeout = os.environ.get("CBAC_TIMEOUT_SECONDS", "3600")
    print(
        f"Starting GitHub MCP server on "
        f"http://{settings.mcp_server_host}:{settings.mcp_server_port}/mcp/  "
        f"(CBAC timeout: {cbac_timeout}s)"
    )
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    )
