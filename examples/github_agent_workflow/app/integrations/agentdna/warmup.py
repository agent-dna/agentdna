"""
Eager AgentDNA initialization helpers.

By default, agent and user AgentDNA instances are constructed lazily on first
use. These helpers let entry points (UI, CLI, MCP server) warm up the
registries at startup so the first user request doesn't pay the cold-start
NFT-load cost for every identity.

All functions soft-fail to a no-op when AgentDNA is disabled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import structlog

from app.agent_names import coordinator_name, set_agent_name, worker_name
from app.constants import ANONYMOUS_USER_EMAIL

from .registry import agentdna_registry, is_agentdna_enabled
from .user_session import _get_or_make_user

logger = structlog.get_logger(__name__)


# Paths to the per-agent skills files.
_AGENTS_BASE = Path(__file__).resolve().parents[2] / "agents"
_COORDINATOR_SKILLS = str(_AGENTS_BASE / "coordinator" / "skills.md")
_WORKER_SKILLS      = str(_AGENTS_BASE / "worker"      / "skills.md")

# Persisted agent-DID map (project root). Overwritten on every warmup.
_PROJECT_ROOT   = Path(__file__).resolve().parents[3]
AGENT_DIDS_FILE = _PROJECT_ROOT / "agent_dids.json"


def _known_agents() -> tuple:
    """(alias, skills) per agent, with aliases resolved at call time."""
    return (
        (coordinator_name(), _COORDINATOR_SKILLS),
        (worker_name(),      _WORKER_SKILLS),
    )


# ── Agent kind ────────────────────────────────────────────────────────────────

def warmup_coordinator() -> None:
    """Construct (and cache) the Coordinator's agent AgentDNA."""
    if not is_agentdna_enabled():
        return
    try:
        dna = agentdna_registry.get(coordinator_name(), policy_file=_COORDINATOR_SKILLS)
        if dna:
            logger.info("agentdna_warmup_coordinator", did=getattr(dna, "did", None))
    except Exception as exc:
        logger.warning("agentdna_warmup_coordinator_failed", error=str(exc))


def warmup_worker() -> None:
    """Construct (and cache) the Worker's agent AgentDNA."""
    if not is_agentdna_enabled():
        return
    try:
        dna = agentdna_registry.get(worker_name(), policy_file=_WORKER_SKILLS)
        if dna:
            logger.info("agentdna_warmup_worker", did=getattr(dna, "did", None))
    except Exception as exc:
        logger.warning("agentdna_warmup_worker_failed", error=str(exc))


def warmup_agents() -> None:
    """Construct both Coordinator and Worker agent AgentDNAs and persist their DIDs."""
    warmup_coordinator()
    warmup_worker()
    dump_agent_dids()


# ── DID persistence ───────────────────────────────────────────────────────────

def dump_agent_dids(path: Optional[Path] = None) -> None:
    """
    Write the registered agent name → DID map to a JSON file (overwrite mode).

    Called automatically at the end of ``warmup_agents()``. Safe to call
    multiple times — each call rewrites the file. No-op when AgentDNA is
    disabled.

    File format:
        {
          "coordinator_agent": "did:rubix:...",
          "worker_agent":      "did:rubix:..."
        }
    """
    if not is_agentdna_enabled():
        return

    target = Path(path) if path else AGENT_DIDS_FILE

    dids: dict[str, str] = {}
    for name, skills in _known_agents():
        try:
            dna = agentdna_registry.get(name, policy_file=skills)
            if dna:
                did = getattr(dna, "did", None)
                if did:
                    dids[name] = did
        except Exception as exc:
            logger.warning("agentdna_did_lookup_failed", agent=name, error=str(exc))

    if not dids:
        return

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dids, indent=2) + "\n", encoding="utf-8")
        logger.info("agent_dids_written", path=str(target), count=len(dids))
    except Exception as exc:
        logger.warning("agent_dids_write_failed", path=str(target), error=str(exc))


# ── User kind ─────────────────────────────────────────────────────────────────

def warmup_user(email: Optional[str] = None) -> None:
    """
    Construct (and cache) a kind="user" AgentDNA for the given email.

    Only the identity NFT is loaded here — the per-request intent envelope
    is still produced by ``UserSession.open()`` at submission time.
    """
    if not is_agentdna_enabled():
        return
    email = (
        email
        or os.environ.get("GITHUB_AGENT_USER_EMAIL")
        or ANONYMOUS_USER_EMAIL
    )
    metadata = {
        "email": email,
        "orgId": os.environ.get("ORGANISATION_ID", ""),
    }
    try:
        user = _get_or_make_user(email, metadata=metadata)
        if user:
            logger.info("agentdna_warmup_user", email=email, did=user.did)
    except Exception as exc:
        logger.warning("agentdna_warmup_user_failed", email=email, error=str(exc))


# ── One-shot ──────────────────────────────────────────────────────────────────

def warmup_all(user_email: Optional[str] = None) -> None:
    """Warm up both agent identities + the default user identity."""
    warmup_agents()
    warmup_user(user_email)


# ── UI-driven deploy helpers (return a status dict for display) ────────────────

def deploy_agent(role: str, name: str) -> dict:
    """
    Deploy a kind="agent" AgentDNA under a UI-chosen ``name`` and return a
    status dict for the UI.

    ``role`` is ``"coordinator"`` or ``"worker"`` (selects the skills file);
    ``name`` is the chosen alias. The name is persisted to ``agent_names.json``
    first, so the MCP server and graph nodes resolve the same identity (same
    alias → same DID). Idempotent.

    Returns ``{"ok": True, "kind", "role", "alias", "did", "nft_token"}`` on
    success, else ``{"ok": False, "reason": "..."}``.
    """
    if not is_agentdna_enabled():
        return {"ok": False, "reason": "AgentDNA disabled — set AGENTDNA_API_KEY"}

    skills = {
        "coordinator": _COORDINATOR_SKILLS,
        "worker":      _WORKER_SKILLS,
    }.get(role)
    if skills is None:
        return {"ok": False, "reason": f"unknown role {role!r}"}

    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "agent name is required"}

    # Persist the chosen alias first, so every process resolving names agrees.
    set_agent_name(role, name)

    try:
        dna = agentdna_registry.get(name, policy_file=skills)
        if dna is None:
            return {"ok": False, "reason": "registry returned None"}
        dump_agent_dids()  # keep agent_dids.json in sync
        return {
            "ok": True,
            "kind": "agent",
            "role": role,
            "alias": name,
            "did": getattr(dna, "did", None),
            "nft_token": getattr(dna, "nft_token", None),
        }
    except Exception as exc:
        logger.warning("agentdna_deploy_agent_failed", role=role, name=name, error=str(exc))
        return {"ok": False, "reason": str(exc)}


def deploy_user(email: str) -> dict:
    """
    Construct (and cache) the kind="user" AgentDNA for ``email`` (taken from UI
    input, not env) and return a status dict for the UI.

    Returns ``{"ok": True, "kind", "alias", "did", "nft_token"}`` on success,
    else ``{"ok": False, "reason": "..."}``.
    """
    if not is_agentdna_enabled():
        return {"ok": False, "reason": "AgentDNA disabled — set AGENTDNA_API_KEY"}

    email = (email or "").strip()
    if not email:
        return {"ok": False, "reason": "email is required"}

    metadata = {
        "email": email,
        "orgId": os.environ.get("ORGANISATION_ID", ""),
    }
    try:
        user = _get_or_make_user(email, metadata=metadata)
        if user is None:
            return {"ok": False, "reason": "could not construct user identity"}
        return {
            "ok": True,
            "kind": "user",
            "alias": email,
            "did": getattr(user, "did", None),
            "nft_token": getattr(user, "nft_token", None),
        }
    except Exception as exc:
        logger.warning("agentdna_deploy_user_failed", email=email, error=str(exc))
        return {"ok": False, "reason": str(exc)}
