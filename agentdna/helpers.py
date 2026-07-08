import json
import hashlib

from .types import (
    Envelope,
    IntentWorkflow,
    Actor,
    Issue
)

def canonicalize_envelope(envelope: Envelope) -> str:
    """
    Produces the canonical representation used
    for both signing and verification.

    Ancestor signatures are included.

    Returns the SHA-256 hash of the envelope
    """

    envelope_dict = _envelope_to_dict(envelope)

    envelope_dict_str = json.dumps(
        envelope_dict,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(envelope_dict_str).hexdigest()

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
) -> Envelope | None:
    """
    Returns the root envelope from a workflow.
    """

    if workflow.envelope is None:
        raise ValueError(
            "workflow does not contain an envelope"
        )

    current = parse_envelope(workflow.envelope)

    while current.parent_envelope is not None:
        current = parse_envelope(current.parent_envelope)

    return current

def get_envelope_depth(
    envelope: Envelope | None,
) -> int:
    depth = 0

    while envelope:
        depth += 1
        envelope = envelope.parent_envelope

    return depth

def unwrap_workflow(workflow: IntentWorkflow) -> list[Envelope]:
    """
    Unwraps a workflow into a list of envelopes.

    Returns envelopes in descending order:
        [latest ... root]

    Example:
        E4(E3(E2(E1)))

    becomes:
        [E4, E3, E2, E1]
    """
    current = get_latest_envelope(workflow)
    envelopes: list[Envelope] = []

    while current is not None:
        if isinstance(current, dict):
            current = parse_envelope(current)
        envelopes.append(current)
        current = current.parent_envelope

    return envelopes

def _envelope_to_dict(envelope: Envelope, is_current=True) -> dict:
    """
    Converts an envelope into a canonical dictionary.

    Parent envelope signatures are always included.

    `is_current` skips adding the signature attribute to the dict
    if envelope is current.

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
        "epoch": envelope.epoch,
        "issues": [
            {
                "depth": issue.depth,
                "reason": issue.reason
            } for issue in envelope.issues
        ]
    }

    if not is_current:
        result["signature"] = envelope.signature

    if envelope.parent_envelope is not None:
        result["parent_envelope"] = (
            _envelope_to_dict(envelope.parent_envelope, is_current=False)
        )

    return result

def parse_workflow(data: dict | IntentWorkflow) -> IntentWorkflow:
    if isinstance(data, IntentWorkflow):
        return data
    data = dict(data)
    data["envelope"] = parse_envelope(data.get("envelope"))
    return IntentWorkflow(**data)

def parse_envelope(data: dict | Envelope | None) -> Envelope | None:
    """Recursively turns a raw dict (and any nested dicts) into a
    proper Envelope, including the parent_envelope chain."""
    if data is None or isinstance(data, Envelope):
        return data

    data = dict(data)  # don't mutate caller's dict

    # handle "from" alias
    if "from" in data and "from_" not in data:
        data["from_"] = data.pop("from")

    data["from_"] = parse_actor(data.get("from_"))
    data["to"] = parse_actor(data.get("to"))
    data["issues"] = [parse_issue(i) for i in data.get("issues", [])]
    data["parent_envelope"] = parse_envelope(data.get("parent_envelope"))

    return Envelope(**data)

def parse_actor(data: dict | Actor | None) -> Actor | None:
    if data is None or isinstance(data, Actor):
        return data
    return Actor(**data)

def parse_issue(data: dict | Issue) -> Issue:
    if isinstance(data, Issue):
        return data
    return Issue(**data)