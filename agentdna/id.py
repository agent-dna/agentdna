import hashlib
from multiformats_cid.cid import CIDv0

def get_agent_card_id(agent_name: str) -> str:
    """
    Get a deterministic Agent Card ID based on the provided agent name.
    """

    return get_id(f"agent_card:{agent_name}")

def get_user_card_id(user_name: str) -> str:
    """
    Get a deterministic User Card ID based on the provided user name.
    """

    return get_id(f"user_card:{user_name}")

def get_id(name: str) -> str:
    """
    Get a deterministic ID based on the provided name.
    """

    digest = hashlib.sha256(f"{name}".encode("utf-8")).digest()
    multihash_bytes = bytes([0x12, len(digest)]) + digest
    return CIDv0(multihash_bytes).encode().decode("utf-8")