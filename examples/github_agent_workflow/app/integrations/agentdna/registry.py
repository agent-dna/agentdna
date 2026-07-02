"""
Singleton registry of AgentDNA identities for GithubAgent.

Each agent alias maps to a single AgentDNA instance constructed lazily and
cached for the lifetime of the process.

Agent identities are persisted by AgentDNA, so resolving the same alias across
multiple processes (Streamlit, LangGraph, MCP server, CLI, etc.) always yields
the same Actor ID and Agent Card. This allows every component of the system to
independently recover an agent's identity while participating in the same
verified workflow.
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
        self._api_key = os.environ.get("AGENTDNA_API_KEY")

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

    def find(
        self,
        agent_name: str,
    ) -> AgentDNA | None:
        """
        Lookup an AgentDNA instance without constructing it.

        Returns:
            Cached AgentDNA instance if present.

            None otherwise.
        """
        if not is_agentdna_enabled():
            return None

        return self._cache.get(agent_name)

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
