from __future__ import annotations

from config import settings
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

from agentdna.mcp.relay import ProvenanceRelay

# Signs the workers' MCP tool calls. Runs inside this process; one relay
# serves every worker, each through a session of its own.
PROVENANCE_RELAY = ProvenanceRelay({"research": settings.research_mcp_url})


async def load_tools(mcp_url: str) -> list[BaseTool]:
    """The research tools. `mcp_url` is a relay session's URL, which signs the tool calls."""

    client = MultiServerMCPClient(
        {
            "research": StreamableHttpConnection(
                transport="streamable_http",
                url=mcp_url,
                timeout=settings.http_timeout_seconds,
                sse_read_timeout=settings.http_timeout_seconds,
            )
        }
    )
    return await client.get_tools()
