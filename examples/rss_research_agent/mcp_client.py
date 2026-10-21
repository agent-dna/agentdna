from __future__ import annotations

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from agentdna.mcp.relay import ProvenanceRelay
from config import settings

# Signs the worker agents' MCP tool calls. Runs inside this process and is
# shared by both workers: each opens its own session, so each signs as itself.
PROVENANCE_RELAY = ProvenanceRelay({"rss": settings.rss_mcp_url})


def build_client(mcp_url: str) -> MultiServerMCPClient:
    """A plain MCP client. `mcp_url` is an AgentDNA Provenance Relay session URL, which signs its tool calls."""
    return MultiServerMCPClient(
        {
            "rss": StreamableHttpConnection(
                transport="streamable_http",
                url=mcp_url,
                timeout=settings.mcp_timeout_seconds,
                sse_read_timeout=settings.mcp_timeout_seconds,
            )
        },
    )


async def load_tools(mcp_url: str) -> list[BaseTool]:
    """Discover the configured RSS server's tools; direct network access is prohibited for agents."""
    return await build_client(mcp_url).get_tools()
