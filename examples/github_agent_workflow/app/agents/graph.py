"""LangGraph state graph wiring Coordinator → Worker."""

from __future__ import annotations

from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.coordinator import coordinator_node
from app.agents.worker import worker_node
from app.agents.state import GithubAgentState


def route_after_coordinator(state: GithubAgentState) -> str:
    if state.get("_agentdna_terminal", False):
        return "end"

    return "worker"


def build_graph():
    graph = StateGraph(GithubAgentState)
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("worker", worker_node)

    graph.add_edge(START, "coordinator")
    graph.add_conditional_edges(
        "coordinator",
        route_after_coordinator,
        {
            "worker": "worker",
            "end": END,
        },
    )
    graph.add_edge("worker", END)

    return graph.compile()
