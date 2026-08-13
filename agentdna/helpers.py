import hashlib
import json
from typing import overload

from .log import get_logger
from .types import Envelope, IntentWorkflow

logger = get_logger("agentdna.helpers")


def _hash_content(envelope: Envelope) -> str:
    """
    Extracts and hashes ONLY the intrinsic fields of the
    current envelope. Explicitly ignores 'hash', 'signature', and 'parent_envelope'.
    """
    content = {
        "from_": envelope.from_,
        "payload": envelope.payload,
        "epoch": envelope.epoch,
        "status_code": envelope.status_code,
        "run_id": envelope.run_id,
        "to": envelope.to,
    }

    content_str = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")

    return hashlib.sha256(content_str).hexdigest()


def canonicalize_envelope(envelope: Envelope) -> str:
    """
    Combines the content hash with the parent hashes
    to create a strict, flat canonical hash for signing.
    """
    content_hash = _hash_content(envelope)

    if not envelope.hash:
        envelope.hash = content_hash

    if not envelope.parent_envelope or len(envelope.parent_envelope) == 0:
        return content_hash

    parent_hashes = []
    for parent in envelope.parent_envelope:
        if parent.hash == "":
            error_msg = f"Parent envelope {parent} lacks hash; cannot canonicalize."
            logger.error("agentdna.helpers.canonicalize_envelope", msg=error_msg)
            raise ValueError(error_msg)

        parent_hashes.append(parent.hash)

    parent_hashes.sort()  # To ensure deterministic ordering of parent hashes

    combined_msg = {"content_hash": content_hash, "parents": parent_hashes}

    combined_str = json.dumps(combined_msg, sort_keys=True, separators=(",", ":")).encode("utf-8")

    return hashlib.sha256(combined_str).hexdigest()


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

    if "from" in data and "from_" not in data:
        data["from_"] = data.pop("from")

    parents_data = data.get("parent_envelope")
    if parents_data:
        if isinstance(parents_data, dict):
            parents_data = [parents_data]
        data["parent_envelope"] = [parse_envelope(p) for p in parents_data if p]
    else:
        data["parent_envelope"] = None

    # Safety check: Allow "hash" to pass through deserialization
    allowed_keys = {
        "from_",
        "payload",
        "epoch",
        "status_code",
        "run_id",
        "to",
        "hash",
        "signature",
        "parent_envelope",
    }
    data = {k: v for k, v in data.items() if k in allowed_keys}

    return Envelope(**data)
