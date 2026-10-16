"""Package-intelligence tools, as an AgentDNA-protected MCP server.

The tools themselves are in package_tools.py; every one uses public data that
needs no API key. AgentDNA's middleware requires every tool call to carry a
signed IntentWorkflow from a whitelisted agent, and signs each answer.

    python mcp_server.py
"""

from __future__ import annotations

from config import settings
from fastmcp import FastMCP
from package_tools import TOOLS

from agentdna import AgentDNA
from agentdna.mcp.server.fastmcp import AgentDNAMCPMiddleware

mcp_server_dna = AgentDNA(
    name=settings.mcp_server_name,
    type="tool",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
    admin_server_url=settings.admin_server_url,
)

mcp = FastMCP("package-intelligence")
mcp.add_middleware(AgentDNAMCPMiddleware(mcp_server_dna))

for tool in TOOLS:
    mcp.tool(tool)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port, path="/mcp"
    )
