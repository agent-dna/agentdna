from __future__ import annotations

import json

from agentdna.types import IntentWorkflow, load_workflow


AGENTDNA_HEADER_NAME = "x-agentdna-intent-workflow"
AGENTDNA_META_KEY = "agentdna"
AGENTDNA_INTENT_WORKFLOW_META_KEY = "intent_workflow"


def workflow_to_header(
    workflow: IntentWorkflow,
) -> str:
    """
    Serialize an IntentWorkflow for transport in an HTTP header.
    """

    return workflow.serialize()


def workflow_from_header(
    value: str | None,
) -> IntentWorkflow | None:
    """
    Deserialize an IntentWorkflow transported as an HTTP header.
    """

    if not value:
        return None

    workflow_data = json.loads(
        value
    )

    return load_workflow(
        workflow_data
    )