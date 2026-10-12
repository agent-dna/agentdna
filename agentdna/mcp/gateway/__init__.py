"""AgentDNA MCP Gateway.

Moves IntentWorkflow propagation out of framework-specific MCP client patches
and onto the MCP protocol boundary, so any MCP client works unmodified:

    MCP_GATEWAY = MCPGateway({"github": "http://127.0.0.1:8011/mcp"})

    async with MCP_GATEWAY.session(AGENT, incoming_workflow) as session:
        ...  # point any MCP client at session.url("github")
    reply = AGENT.build(payload=output, previous_workflows=session.workflows)

The gateway runs inside the agent's process, on a background thread, and
starts on first use.
"""

from .client import GatewayError, GatewaySession, MCPGateway, WorkflowRejected

__all__ = ["GatewayError", "GatewaySession", "MCPGateway", "WorkflowRejected"]
