import hashlib
import uuid

from multiformats_cid.cid import CIDv0


def get_agent_card_id(agent_id: str):
    """
    Get a deterministic Agent Card ID based on the provided agent name.
    """
    return get_id(agent_id)


def get_user_card_id(user_id: str):
    """
    Get a deterministic User Card ID based on the provided agent name.
    """
    return get_id(user_id)


def get_id(val: str):
    """
    Get a deterministic ID based on the provided value.
    """
    digest = hashlib.sha256(f"{val}".encode()).digest()
    multihash_bytes = bytes([0x12, len(digest)]) + digest
    return CIDv0(multihash_bytes).encode().decode("utf-8")


def get_intent_workflow_id(user_card_id: str):
    """
    Get a deterministic Intent Workflow ID based on the provided user card ID.
    """
    return f"{user_card_id}-{uuid.uuid4()}"
