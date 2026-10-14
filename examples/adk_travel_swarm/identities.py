"""The AgentDNA identity of each agent in the swarm.

Each ADK agent signs as itself: its MCP tool calls through its own gateway
session, and the hand-offs it makes to its peers.
"""

from __future__ import annotations

from pathlib import Path

from config import settings

from agentdna.core import AgentDNA

POLICIES = Path(__file__).resolve().parent / "policies"


def _agent(name: str, policy: str) -> AgentDNA:
    return AgentDNA(
        name=name,
        type="agent",
        agent_policy_file=POLICIES / policy,
        api_key=settings.agentdna_api_key,
        provenance_layer_url=settings.provenance_layer_url,
        admin_server_url=settings.admin_server_url,
    )


# Keyed by the ADK agent name.
IDENTITIES = {
    "coordinator": _agent(settings.coordinator_agent_name, "coordinator.md"),
    "weather_agent": _agent(settings.weather_agent_name, "weather_agent.md"),
    "destination_agent": _agent(settings.destination_agent_name, "destination_agent.md"),
    "money_agent": _agent(settings.money_agent_name, "money_agent.md"),
}
