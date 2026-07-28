"""
Run the guarded agentic flow end-to-end from the CLI (no UI, no MCP server).

Same flow as run_flow.py — user → coordinator → worker → GitHub — but the
worker's GitHub tools are in-process functions wrapped by @cbac_guard
(see app/agents/worker_guarded.py). No separate MCP process is needed.

Usage:
    python scripts/run_flow_guarded.py "Create an issue in owner/repo titled 'Bug' with body '...'"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END, START, StateGraph

from agentdna.cbac import configure

from app.agent_names import coordinator_name
from app.agents.coordinator import coordinator_node
from app.agents.graph import route_after_coordinator
from app.agents.state import GithubAgentState
from app.agents.worker_guarded import worker_node
from app.constants import ANONYMOUS_USER_EMAIL
from app.integrations.agentdna import UserSession, warmup_all
from app.agents.agentdna_helpers import get_dna
from app.utils import deserialize_workflow, serialize_workflow

COORDINATOR_SKILLS_FILE = str(
    Path(__file__).resolve().parent.parent / "app" / "agents" / "coordinator" / "skills.md"
)


def build_graph():
    graph = StateGraph(GithubAgentState)
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("worker", worker_node)
    graph.add_edge(START, "coordinator")
    graph.add_conditional_edges(
        "coordinator",
        route_after_coordinator,
        {"worker": "worker", "end": END},
    )
    graph.add_edge("worker", "coordinator")
    return graph.compile()


async def main(user_input: str) -> None:
    configure(
        cbac_url=os.environ.get("CBAC_URL", "https://cbac-admin.agentdna.io"),
        cbac_timeout=float(os.environ.get("CBAC_TIMEOUT", "103600")),
    )

    user_email = os.environ.get("GITHUB_AGENT_USER_EMAIL", ANONYMOUS_USER_EMAIL)

    warmup_all(user_email=user_email)

    coordinator_dna = get_dna(coordinator_name(), COORDINATOR_SKILLS_FILE)
    if coordinator_dna is None:
        raise RuntimeError("failed to load coordinator AgentDNA")

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

    initial_state = {"user_input": user_input}

    if user_session is not None:
        initial_state["_agentdna_workflow"] = serialize_workflow(user_session.workflow)
        initial_state["_agentdna_user_id"] = user_session.user_id
        print(f"[agentdna] user_did={user_session.user_id}\n")

    result = await graph.ainvoke(initial_state)

    workflow = result.get("_agentdna_workflow")

    if workflow and user_session:
        print("─── Final Workflow ───────────────────────────────────────")
        completed = user_session.complete(deserialize_workflow(workflow))
        print(f"[agentdna] provenance written = {completed}")

    print("─── Coordinator output ─────────────────────────────────────")
    print(result.get("task_spec", "(empty)"))

    print("\n─── Worker transcript ─────────────────────────────────────")
    for msg in result.get("worker_messages", []):
        role = getattr(msg, "type", "unknown")
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            content = str(content)
        print(f"[{role}] {content}")

    print("\n─── Final response ─────────────────────────────────────────")
    print(result.get("final_response", "(none)"))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/run_flow_guarded.py '<request>'")
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))
