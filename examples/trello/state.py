"""Shared workflow state.

The state is the single source of truth shared between all LangGraph nodes.

Messages are retained for LLM context, while workflow-specific information is
stored in dedicated fields so nodes don't need to inspect message history to
understand what has already happened.
"""
from __future__ import annotations

from typing import Any
from typing_extensions import Annotated, TypedDict
from agentdna.types import IntentWorkflow

from langgraph.graph.message import add_messages
from uuid import uuid4
from langchain_core.messages import HumanMessage

def make_initial_state(
    user_input: str,
    adna_envelope: IntentWorkflow
) -> WorkflowState:

    return {
        "messages": [
            HumanMessage(content=user_input),
        ],
        "workflow_name": "release_announcer",
        "request_id": str(uuid4()),
        "tool_result": None,
        "success": False,
        "error": None,
        "user_query": user_input,
        "adna_envelope": adna_envelope,
    }

class WorkflowState(TypedDict):
    """State shared across the workflow."""

    # Conversation history used by the LLM.
    messages: Annotated[list, add_messages]
    user_query: str

    # Metadata.
    workflow_name: str
    request_id: str

    # Tool execution.
    tool_result: dict[str, Any] | None

    # Execution status.
    success: bool
    error: str | None

    # (ADNA) Data Transfer Object (DTO) for the AgentDNA workflow's intent. This is the envelope that is passed to the workflow's intent handler.
    adna_envelope: IntentWorkflow