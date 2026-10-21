"""Package-intelligence tools, as an AgentDNA-protected MCP server.

The tools themselves are in package_tools.py; every one uses public data that
needs no API key. AgentDNA's middleware requires every tool call to carry a
signed IntentWorkflow from a whitelisted agent, has the CBAC service authorize
the call against the user's intent, and signs each answer.

    python mcp_server.py
"""

from __future__ import annotations

from functools import partial

from cbac import authorize
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

# `authorize` sends every decision to its built-in default service, and ignores
# the CBAC_URL environment variable. Bind CBAC_URL, when it is set (for a dev
# CBAC service, say), so the decisions go there instead.
cbac_fn = partial(authorize, cbac_url=settings.cbac_url) if settings.cbac_url else authorize

mcp = FastMCP("package-intelligence")
mcp.add_middleware(AgentDNAMCPMiddleware(mcp_server_dna, cbac_fn=cbac_fn))

for tool in TOOLS:
    mcp.tool(tool)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port, path="/mcp"
    )
