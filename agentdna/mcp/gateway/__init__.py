"""AgentDNA MCP Gateway.

Moves IntentWorkflow propagation out of the agent process and onto the MCP
boundary, so any MCP client works without a framework-specific adapter.

    agent app ── POST /runs (incoming workflow) ──▶ gateway
    MCP client ── /runs/{id}/mcp ──▶ gateway ── + _meta.agentdna ──▶ MCP server
    agent app ── DELETE /runs/{id} ──▶ gateway ── final frontier ──▶ agent app

The client side (`GatewayClient`) needs only httpx. The server side needs the
`gateway` extra: `pip install 'agent-dna[gateway]'`.
"""

from .client import GatewayClient, GatewayError, GatewayRun

__all__ = ["GatewayClient", "GatewayError", "GatewayRun"]
