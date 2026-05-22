"""
user-kind helpers for AgentDNA.

Functions that produce the on-chain identity payload for a *user* and
deploy it as an NFT. ``core.AgentDNA`` exposes thin wrappers
(``deploy_user_nft`` / ``_identity_nft_payload``) that delegate here
based on ``self.kind``.
"""

from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .core import AgentDNA


def identity_payload(dna: "AgentDNA") -> Dict[str, Any]:
    """
    Build the on-chain payload for a user's identity NFT::

        {"user_did", "metadata"}

    ``metadata`` is a free-form dict supplied via ``metadata=`` at init
    (defaults to ``{}``).
    """
    return {
        "user_did": dna.did,
        "metadata": dna.metadata,
    }


def deploy_user_nft(dna: "AgentDNA") -> str:
    """
    Publish the user's identity NFT on Rubix. Idempotent — returns the
    cached address if this identity has already been minted.

    Raises ``RuntimeError`` if ``dna.kind`` is not ``"user"``.
    """
    if dna.kind != "user":
        raise RuntimeError(
            f"deploy_user_nft() is for kind='user'; this AgentDNA is "
            f"kind={dna.kind!r}. Agents call deploy_card()."
        )
    return dna._publish_identity_nft()
