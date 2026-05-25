"""
agent-kind helpers for AgentDNA.

Functions that produce the on-chain identity payload for an *agent* and
deploy it as an NFT. ``core.AgentDNA`` exposes thin wrappers
(``deploy_card`` / ``_identity_nft_payload``) that delegate here based
on ``self.kind``.
"""

from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .core import AgentDNA


def identity_payload(dna: "AgentDNA") -> Dict[str, Any]:
    """
    Build the on-chain payload for an agent's identity NFT::

        {"agent_did", "agent_metadata", "policy", "deployer"}

    ``policy`` is the base64-encoded contents of the file at
    ``policy_file=`` (``""`` if none was passed to the constructor). The
    file is opaque to AgentDNA — projects choose their own format
    (markdown, JSON, plain text, etc.).
    """
    return {
        "type":           "agent_nft",
        "agent_did":      dna.did,
        "agent_metadata": dna.metadata,
        "policy":         dna.policy,
    }


def deploy_card(dna: "AgentDNA") -> str:
    """
    Publish the agent's identity NFT on Rubix. Idempotent — returns the
    cached address if this identity has already been minted.

    Raises ``RuntimeError`` if ``dna.kind`` is not ``"agent"``.
    """
    if dna.kind != "agent":
        raise RuntimeError(
            f"deploy_card() is for kind='agent'; this AgentDNA is "
            f"kind={dna.kind!r}. Users call deploy_user_nft()."
        )
    return dna._publish_identity_nft()
