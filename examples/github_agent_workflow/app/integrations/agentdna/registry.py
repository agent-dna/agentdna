"""
Singleton registry of AgentDNA identities for GithubAgent.

Mirrors the FinanceOps-MAS registry pattern. Each agent_name maps to a single
AgentDNA constructed lazily and reused per process. Because identity NFTs are
cached on disk (via AgentDNA's agent_info.json), the same alias resolves to
the same DID across processes — so the MCP server can look up the Worker's
identity independently of the LangGraph process.

If AGENTDNA_API_KEY is not set, ``get()`` returns ``None`` for every agent —
the LangGraph nodes and MCP tools see that and skip the trust layer, so the
underlying flow runs unchanged.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional
from agentdna.core import AgentDNA

import structlog

logger = structlog.get_logger(__name__)

def is_agentdna_enabled() -> bool:
    """True iff an API key is configured in the environment."""
    return bool(os.environ.get("AGENTDNA_API_KEY"))

class AgentDNARegistry:
    """Process-wide singleton store for AgentDNA identities."""

    _instance: Optional["AgentDNARegistry"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "AgentDNARegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self._cache: Dict[str, AgentDNA] = {}
        self._api_key = os.environ.get("AGENTDNA_API_KEY") or ""
        self._chain_url = os.environ.get("AGENTDNA_CHAIN_URL")

    # ── Public API ────────────────────────────────────────────────────────

    def get(
        self,
        agent_name: str,
        policy_file: Optional[str] = None,
    ) -> AgentDNA | None:
        """
        Lookup-or-create the AgentDNA instance
        for the supplied agent.

        The instance is cached per process.
        Agent registration and card creation
        are handled internally by AgentDNA.
        """
        if not is_agentdna_enabled():
            return None

        if agent_name in self._cache:
            return self._cache[agent_name]

        with self._lock:
            if agent_name in self._cache:
                return self._cache[agent_name]
            instance = self._construct(agent_name, policy_file)
            self._cache[agent_name] = instance
            return instance

    # ── Internals ─────────────────────────────────────────────────────────

    def _construct(
        self,
        agent_name: str,
        policy_file: Optional[str],
    ):
        # Lazy import so callers don't pay AgentDNA's dependency cost when
        # the integration is disabled.
        from agentdna.core import AgentDNA
        
        if not policy_file:
            raise Exception("policy file path must be provided")

        logger.info("agentdna_constructing", agent=agent_name, policy=policy_file)
        instance = AgentDNA(
            name=agent_name,
            type="agent",
            api_key=os.environ.get("AGENTDNA_API_KEY", ""),
            provenance_layer_url=os.environ.get("AGENTDNA_CHAIN_URL", ""),
            agent_policy_file=Path(policy_file)
        )

        logger.info(
            "agentdna_ready",
            agent=agent_name,
            did=instance.get_actor_id(),
        )

        return instance


# Module-level convenience handle.
agentdna_registry = AgentDNARegistry()
