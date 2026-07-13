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
from app.mcp_server.github_mcp import mcp


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    )
