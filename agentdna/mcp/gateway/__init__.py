"""AgentDNA MCP Gateway.

Moves IntentWorkflow propagation out of framework-specific MCP client patches
and onto the MCP protocol boundary, so any MCP client works unmodified:

    MCP = AGENT.mcp_gateway({"github": "http://127.0.0.1:8011/mcp"})

    async with MCP.session(incoming_workflow) as session:
        ...  # point any MCP client at session.url("github")
    reply = session.reply(output)

By default the gateway runs inside the agent's process. It can also run as a
standalone service (`agentdna-gateway`), reached with
`AGENT.mcp_gateway(url=...)`. Either way it needs the `gateway` extra:
`pip install 'agent-dna[gateway]'`.
"""

from .client import GatewayError, GatewaySession, MCPGateway, WorkflowRejected

__all__ = ["GatewayError", "GatewaySession", "MCPGateway", "WorkflowRejected"]
