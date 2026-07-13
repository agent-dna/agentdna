"""
Runtime resolution of agent names (= AgentDNA aliases).

Names can be chosen from the UI at deploy time and are persisted to a
gitignored JSON file in the project root (``agent_names.json``). Every process
— the LangGraph app, the MCP server, the CLI — reads from there, so they all
resolve the SAME alias (and therefore the same on-chain DID) for each agent.

Resolution order per role: names file → env var → hardcoded default
(the env/default come from ``app.constants``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from app.constants import AGENT_COORDINATOR, AGENT_WORKER

# Project root (app/agent_names.py → parents[1]). Sits next to agent_dids.json.
AGENT_NAMES_FILE = Path(__file__).resolve().parents[1] / "agent_names.json"

# Fallbacks when the file has no entry for a role.
_DEFAULTS: Dict[str, str] = {
    "coordinator": AGENT_COORDINATOR,
    "worker": AGENT_WORKER,
}


def _read() -> Dict[str, str]:
    """Load the names map; empty dict when the file is missing/unreadable."""
    try:
        data = json.loads(AGENT_NAMES_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def agent_name(role: str) -> str:
    """Resolve the alias for ``role`` ('coordinator' | 'worker')."""
    name = (_read().get(role) or "").strip()
    return name or _DEFAULTS.get(role, role)


def coordinator_name() -> str:
    return agent_name("coordinator")


def worker_name() -> str:
    return agent_name("worker")


def set_agent_name(role: str, name: str) -> Dict[str, str]:
    """
    Persist ``name`` as the alias for ``role`` and return the full map.

    Called at deploy time so the MCP server and graph nodes resolve the same
    alias the UI just used.
    """
    data = _read()
    data[role] = (name or "").strip()
    AGENT_NAMES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
