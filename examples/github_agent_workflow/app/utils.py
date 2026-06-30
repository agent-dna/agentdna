import json

from dataclasses import asdict

from agentdna.types import (
    IntentWorkflow,
    Issue,
    Actor,
    Envelope,
)

def serialize_workflow(
    workflow: IntentWorkflow,
) -> str:
    """
    Serializes an IntentWorkflow into JSON.
    """

    return json.dumps(
        asdict(workflow),
        sort_keys=True,
        separators=(",", ":"),
    )

def deserialize_workflow(
    workflow_json: str,
) -> IntentWorkflow:
    """
    Deserializes an IntentWorkflow from JSON.
    """

    data = json.loads(
        workflow_json
    )

    return IntentWorkflow(
        type=data["type"],
        version=data["version"],
        remarks=data["remarks"],
        info=data.get(
            "info",
            {},
        ),
        envelope=_deserialize_envelope(
            data.get(
                "envelope"
            )
        ),
    )

def _deserialize_envelope(
    data: dict | None,
) -> Envelope | None:
    if data is None:
        return None

    return Envelope(
        from_=Actor(
            **data["from_"]
        ),
        to=Actor(
            **data["to"]
        ),
        payload=data["payload"],
        epoch=data["epoch"],
        metadata=data.get(
            "metadata",
            {},
        ),
        issues=[
            Issue(**issue)
            for issue in data.get(
                "issues",
                []
            )
        ],
        signature=data.get(
            "signature",
            "",
        ),
        parent_envelope=_deserialize_envelope(
            data.get(
                "parent_envelope"
            )
        ),
    )