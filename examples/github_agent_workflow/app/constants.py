"""Centralized constants for GithubAgent."""

import os

from dotenv import load_dotenv

load_dotenv()


# ── Agent identifiers ─────────────────────────────────────────────────────────
# Override via env if you want non-default aliases on the chain.
AGENT_COORDINATOR = os.environ.get("AGENT_COORDINATOR", "coordinator_agent")
AGENT_WORKER      = os.environ.get("AGENT_WORKER",      "worker_agent")

ALL_AGENTS = [AGENT_COORDINATOR, AGENT_WORKER]


# ── User identity defaults ────────────────────────────────────────────────────
ANONYMOUS_USER_EMAIL = "anonymous@github-agent"
