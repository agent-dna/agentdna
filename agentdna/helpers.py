import hashlib
import json
from typing import overload

from .types import Envelope, IntentWorkflow


def canonicalize_envelope(envelope: Envelope) -> str:
    """
    Produces the canonical representation used
    for both signing and verification.

    Ancestor signatures are included.
    Returns the SHA-256 hash of the envelope.
    """
    envelope_dict = _envelope_to_dict(envelope)

    envelope_dict_str = json.dumps(
        envelope_dict,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(envelope_dict_str).hexdigest()


def _envelope_to_dict(envelope: Envelope, is_current=True) -> dict:
    """
    Converts an envelope into a canonical dictionary.

    Performs FULL DEEP RECURSION. Every envelope embeds its complete
    historical lineage.
    """
    result = {
        "from_": envelope.from_,
        "payload": envelope.payload,
        "epoch": envelope.epoch,
        "code": envelope.code,
        "run_id": envelope.run_id,
        "to": envelope.to,
    }

    # If this is a historical envelope, its signature is part of the sealed record
    if not is_current:
        if hasattr(envelope, "signature") and envelope.signature:
            result["signature"] = envelope.signature

    # DEEP RECURSION: We do NOT return early here. We process the entire chain.
    if envelope.parent_envelope:
        parent_dicts = [
            _envelope_to_dict(parent, is_current=False) for parent in envelope.parent_envelope
        ]

        # Sort to guarantee deterministic hashing at merge junctions
        # We fallback to payload if signature is missing during test mock setups
        result["parent_envelope"] = sorted(
            parent_dicts, key=lambda p: p.get("signature", "") or p.get("payload", "")
        )

    return result


def parse_workflow(data: dict | IntentWorkflow) -> IntentWorkflow:
    if isinstance(data, IntentWorkflow):
        return data
    data = dict(data)
    data["envelope"] = parse_envelope(data.get("envelope"))
    return IntentWorkflow(**data)


@overload
def parse_envelope(data: dict | Envelope) -> Envelope: ...
@overload
def parse_envelope(data: None) -> None: ...
def parse_envelope(data: dict | Envelope | None) -> Envelope | None:
    """
    Recursively turns a raw dict into a proper Envelope,
    safely handling the list of parent envelopes in the DAG.
    """
    if data is None or isinstance(data, Envelope):
        return data

    data = dict(data)  # don't mutate caller's dict

    # handle "from" alias
    if "from" in data and "from_" not in data:
        data["from_"] = data.pop("from")

    # DAG REFACTOR: Handle the list of parent envelopes safely
    parents_data = data.get("parent_envelope")
    if parents_data:
        # Fallback in case a legacy linear dictionary is passed
        if isinstance(parents_data, dict):
            parents_data = [parents_data]

        data["parent_envelope"] = [parse_envelope(p) for p in parents_data if p]
    else:
        data["parent_envelope"] = None

    # Safety: Remove any legacy keys (like 'issues') that might exist in old
    # JSON payloads but aren't in our types.py struct anymore.
    # This prevents TypeError: __init__() got an unexpected keyword argument
    allowed_keys = {
        "from_",
        "payload",
        "epoch",
        "code",
        "run_id",
        "signature",
        "parent_envelope",
        "to",
    }
    data = {k: v for k, v in data.items() if k in allowed_keys}

    return Envelope(**data)
