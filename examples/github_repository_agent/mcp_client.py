from __future__ import annotations

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from config import settings


def build_client(mcp_url: str) -> MultiServerMCPClient:
    """A plain MCP client: AgentDNA propagation is done by the gateway at `mcp_url`."""
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
    """Discover the server-owned GitHub capabilities through the gateway run at `mcp_url`."""
    return await build_client(mcp_url).get_tools()
