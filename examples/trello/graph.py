"""LangGraph definition for the Release Announcer workflow."""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from nodes.agent import make_agent_node
from state import WorkflowState


def build_graph(
    *,
    llm: BaseChatModel,
    tools: list,
):
    """Build the workflow graph."""

    builder = StateGraph(WorkflowState)

    builder.add_node(
        "agent",
        make_agent_node(
            llm=llm,
            tools=tools,
        ),
    )

    builder.add_node(
        "tools",
        ToolNode(tools),
    )

    builder.add_edge(START, "agent")
    builder.add_edge("agent", "tools")
    builder.add_edge("tools", END)

    return builder.compile()