import json

from .types import (
    Envelope,
    IntentWorkflow,
)

def canonicalize_envelope(
    envelope: Envelope,
) -> str:
    """
    Produces the canonical representation used
    for both signing and verification.

    Ancestor signatures are included.
    """

    envelope_dict = _envelope_to_dict(
        envelope,
        include_current_signature=True,
    )

    return json.dumps(
        envelope_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

def get_latest_envelope(
    workflow: IntentWorkflow,
) -> Envelope:
    """
    Returns the latest envelope
    from a workflow.
    """

    if workflow.envelope is None:
        raise ValueError(
            "workflow does not contain an envelope"
        )

    return workflow.envelope

def get_root_envelope(
    workflow: IntentWorkflow,
) -> Envelope:
    """
    Returns the root envelope from a workflow.
    """

    if workflow.envelope is None:
        raise ValueError(
            "workflow does not contain an envelope"
        )

    current = workflow.envelope

    while current.parent_envelope is not None:
        current = current.parent_envelope

    return current

def get_envelope_depth(
    envelope: Envelope | None,
) -> int:
    depth = 0

    while envelope:
        depth += 1
        envelope = envelope.parent_envelope

    return depth

def unwrap_workflow(
    workflow: IntentWorkflow,
) -> list[Envelope]:
    """
    Unwraps a workflow into a list of envelopes.

    Returns envelopes in descending order:
        [latest ... root]

    Example:
        E4(E3(E2(E1)))

    becomes:
        [E4, E3, E2, E1]
    """

    current = get_latest_envelope(
        workflow
    )

    envelopes = []

    while current is not None:
        envelopes.append(
            current
        )

        current = (
            current.parent_envelope
        )

    return envelopes

def _envelope_to_dict(
    envelope: Envelope,
    include_current_signature: bool,
) -> dict:
    """
    Converts an envelope into a canonical dictionary.

    Rules:

    - Current envelope signature is included only when
      include_current_signature=True.

    - Parent envelope signatures are always included.

    This creates a chain-of-attestation where every
    envelope commits to all previously signed envelopes.
    """

    result = {
        "from_": {
            "id": envelope.from_.id,
            "name": envelope.from_.name,
            "type": envelope.from_.type,
            "metadata": envelope.from_.metadata,
        },
        "to": {
            "id": envelope.to.id,
            "name": envelope.to.name,
            "type": envelope.to.type,
            "metadata": envelope.to.metadata,
        },
        "payload": envelope.payload,
        "metadata": envelope.metadata,
    }

    if include_current_signature:
        result["signature"] = envelope.signature

    if envelope.parent_envelope is not None:
        result["parent_envelope"] = (
            _envelope_to_dict(
                envelope.parent_envelope,
                include_current_signature=True,
            )
        )

    return result