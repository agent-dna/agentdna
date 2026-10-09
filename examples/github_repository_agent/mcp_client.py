from __future__ import annotations

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from config import settings


def build_client(mcp_url: str) -> MultiServerMCPClient:
    """A plain MCP client. `mcp_url` is an AgentDNA gateway session URL, which signs its tool calls."""
    return MultiServerMCPClient(
        {
            "github": StreamableHttpConnection(
                transport="streamable_http",
                url=mcp_url,
                timeout=settings.mcp_timeout_seconds,
                sse_read_timeout=settings.mcp_timeout_seconds,
            )
        },
    )


async def load_tools(mcp_url: str) -> list[BaseTool]:
    """Discover the server-owned GitHub capabilities through MCP."""
    return await build_client(mcp_url).get_tools()
