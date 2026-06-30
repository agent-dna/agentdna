"""
Run the agentic flow end-to-end from the CLI (no UI).

Prerequisite:
    python scripts/start_mcp.py

Usage:
    python scripts/run_flow.py "Create an issue in owner/repo titled 'Bug' with body 'Repro steps...'"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_names import coordinator_name
from app.agents.graph import build_graph
from app.constants import ANONYMOUS_USER_EMAIL
from app.integrations.agentdna import (
    UserSession,
    warmup_all,
)
from app.agents.agentdna_helpers import get_dna
from app.utils import serialize_workflow, deserialize_workflow

from pathlib import Path

COORDINATOR_SKILLS_FILE = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "agents"
    / "coordinator"
    / "skills.md"
)

async def main(user_input: str) -> None:
    user_email = os.environ.get(
        "GITHUB_AGENT_USER_EMAIL",
        ANONYMOUS_USER_EMAIL,
    )

    warmup_all(user_email=user_email)

    coordinator_dna = get_dna(
        coordinator_name(),
        str(COORDINATOR_SKILLS_FILE),
    )

    if coordinator_dna is None:
        raise RuntimeError(
            "failed to load coordinator AgentDNA"
        )

    user_session = await UserSession.open(
        intent={
            "intent": "github_task",
            "workflow_type": "github_action",
            "submitted_by": user_email,
            "request_preview": user_input[:200],
        },
        submitted_by=user_email,
        submitter_email=user_email,
        first_agent=coordinator_dna,
    )

    graph = build_graph()

    print(f"\n[user] {user_input}\n")

    initial_state = {
        "user_input": user_input,
    }

    if user_session is not None:
        initial_state["_agentdna_workflow"] = serialize_workflow(
            user_session.workflow
        )
        initial_state["_agentdna_user_id"] = (
            user_session.user_id
        )

        print(
            f"[agentdna] user_did={user_session.user_id}\n"
        )

    result = await graph.ainvoke(initial_state)

    #
    # Print workflow for debugging.
    #
    workflow = result.get("_agentdna_workflow")

    if workflow and user_session:
        print(
            "─── Final Workflow ───────────────────────────────────────"
        )
        completed = user_session.complete(
            deserialize_workflow(workflow)
        )
        print(
            f"[agentdna] provenance written = {completed}"
        )

    print(
        "─── Coordinator output ─────────────────────────────────────"
    )
    print(
        result.get(
            "task_spec",
            "(empty)",
        )
    )

    print(
        "\n─── Worker transcript ─────────────────────────────────────"
    )

    for msg in result.get(
        "worker_messages",
        [],
    ):
        role = getattr(
            msg,
            "type",
            "unknown",
        )

        content = getattr(
            msg,
            "content",
            "",
        )

        if not isinstance(
            content,
            str,
        ):
            content = str(content)

        print(f"[{role}] {content}")

    print(
        "\n─── Final response ─────────────────────────────────────────"
    )

    print(
        result.get(
            "final_response",
            "(none)",
        )
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python scripts/run_flow.py '<request>'"
        )
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))