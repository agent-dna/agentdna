"""
Shared AgentDNA helpers for LangGraph nodes.

AgentDNA v2 workflow:

    deserialize workflow
            ↓
        dna.handle()
            ↓
    verification result
            ↓
    node decides:
        - continue
        - reject
        - terminate

This module intentionally contains only lightweight helpers.
Workflow creation should use AgentDNA.build() directly.
"""

from __future__ import annotations

from typing import Optional

import structlog

from agentdna.core import AgentDNA
from agentdna.types import HandleResult

from app.integrations.agentdna import agentdna_registry
from app.utils import deserialize_workflow
from app.agents.state import GithubAgentState

logger = structlog.get_logger(__name__)


def get_dna(
    agent_name: str,
    skills_file: Optional[str] = None,
) -> AgentDNA | None:
    """
    Lookup or construct the AgentDNA instance
    for the supplied agent.

    Returns None when AgentDNA is disabled.
    """
    return agentdna_registry.get(
        agent_name,
        policy_file=skills_file,
    )

def find_dna(
    agent_name: str,
) -> AgentDNA | None:
    return agentdna_registry.find(agent_name)

def verify_inbound(
    dna: AgentDNA,
    state: GithubAgentState,
) -> HandleResult | None:
    """
    Deserialize and verify the inbound workflow.

    Reads the workflow from:

    _agentdna_workflow

    Returns:
        HandleResult on success.

        None only when:
            - AgentDNA is disabled
            - No workflow exists
            - Workflow deserialization fails
            - AgentDNA raises an exception

    IMPORTANT:

    Invalid verification results are NOT filtered here.
    The caller receives the HandleResult and decides
    whether to continue or reject the workflow.
    """
    if not dna:
        return None

    workflow_str = state.get(
        "_agentdna_workflow"
    )

    if not workflow_str:
        return None

    try:
        workflow = deserialize_workflow(
            workflow_str
        )

        return dna.handle(
            workflow=workflow,
        )

    except Exception as exc:
        logger.warning(
            "agentdna_verify_failed",
            error=str(exc),
        )
        return None