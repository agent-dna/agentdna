"""The AgentDNA identities of the fan-out.

The orchestrator is registered once, with its policy. Workers are spawned at
runtime, as many as the question needs, so each one is a sub-agent of the
orchestrator: it gets its own identity, and inherits the orchestrator's policy
and whitelisting.
"""

from __future__ import annotations

from pathlib import Path

from config import settings

from agentdna import AgentDNA
from agentdna.config import get_actor_registry_entry

POLICY_FILE = Path(__file__).resolve().parent / "policies" / "orchestrator.md"

ORCHESTRATOR = AgentDNA(
    name=settings.orchestrator_agent_name,
    type="agent",
    agent_policy_file=POLICY_FILE,
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
    admin_server_url=settings.admin_server_url,
)


def spawn_worker() -> AgentDNA:
    """A new sub-agent of the orchestrator, ready to sign as itself."""

    worker_id = ORCHESTRATOR.create_sub_agent()

    # create_sub_agent() returns the actor ID; signing needs the name, which
    # the actor registry has under that ID.
    entry = get_actor_registry_entry(ORCHESTRATOR.config_dir, worker_id)
    if entry is None:
        raise RuntimeError(f"sub-agent {worker_id} is missing from the actor registry")

    return AgentDNA(
        name=entry.actor_name,
        type="agent",
        skip_actor_id_registration=True,
        config_dir=ORCHESTRATOR.config_dir,
        api_key=settings.agentdna_api_key,
        provenance_layer_url=settings.provenance_layer_url,
        admin_server_url=settings.admin_server_url,
    )
