"""AgentDNA Provenance Relay.

Signs an agent's MCP tool calls at the protocol boundary, so any MCP client
works unmodified:

    PROVENANCE_RELAY = ProvenanceRelay({"github": "http://127.0.0.1:8011/mcp"})

    async with PROVENANCE_RELAY.session(AGENT, incoming_workflow) as session:
        ...  # point any MCP client at session.url("github")
    reply = AGENT.build(payload=output, previous_workflows=session.workflows)

The relay runs inside the agent's process, on a background thread, and starts
on first use.
"""

from .client import ProvenanceRelay, RelayError, RelaySession, WorkflowRejected

__all__ = ["ProvenanceRelay", "RelayError", "RelaySession", "WorkflowRejected"]
