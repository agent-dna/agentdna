from __future__ import annotations

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from config import settings


def build_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "github": StreamableHttpConnection(
                transport="streamable_http",
                url=settings.github_mcp_url,
                timeout=settings.mcp_timeout_seconds,
                sse_read_timeout=settings.mcp_timeout_seconds,
            )
        }
    )


async def load_tools() -> list[BaseTool]:
    """Discover the server-owned GitHub capabilities through MCP."""
    return await build_client().get_tools()