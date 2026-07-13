"""
Worker agent — receives the Coordinator's task spec and executes it by
calling GitHub MCP tools.

Workflow:

    User
      ↓
    Coordinator
      ↓
    Worker
      ↓
    Github (via MCP)
      ↓
    Worker
      ↓
    Coordinator

The MCP layer is responsible for app-level provenance
(Worker -> Github -> Worker).

The Worker is responsible for:
  - Verifying Coordinator workflow.
  - Executing the task.
  - Receiving the updated workflow from MCP.
  - Appending a Worker -> Coordinator envelope.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent

from agentdna.helpers import (
    get_latest_envelope,
)

from app.agent_names import (
    coordinator_name,
    worker_name,
)

from app.agents.agentdna_helpers import (
    get_dna,
    verify_inbound,
)

from app.llm import make_llm
from app.mcp_client.github_client import get_github_tools
from app.utils import (
    serialize_workflow,
    deserialize_workflow,
)
from app.agents.state import GithubAgentState

SKILLS_FILE = str(Path(__file__).parent / "skills.md")

WORKER_SYSTEM = """
You are the Worker agent.

Available tools:
- create_issue(repo, title, body)
- create_pr(repo, title, body, head, base)

Workflow:
1. Read the Coordinator task specification.
2. Choose the correct GitHub tool.
3. Execute it.
4. Summarize the result.

Do not invent values.

If required information is missing,
stop and explain what is missing.
"""


def _extract_workflow(messages: list) -> str:
    """
    Extract the serialized IntentWorkflow returned by an MCP tool.
    """

    for msg in reversed(messages):
        is_tool = getattr(msg, "type", "") == "tool" or msg.__class__.__name__ == "ToolMessage"

        if not is_tool:
            continue

        content = getattr(msg, "content", None)

        if isinstance(content, str):
            try:
                data = json.loads(content)
                workflow = data.get("workflow")
                if workflow:
                    return workflow
            except Exception:
                pass
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue

                text = part.get("text")

                if not isinstance(text, str):
                    continue

                try:
                    data = json.loads(text)
                    workflow = data.get("workflow")
                    if workflow:
                        return workflow
                except Exception:
                    continue

    return ""


def _wrap_tools(
    tools: list,
    workflow_json: str,
) -> list:
    """
    Inject the current workflow into every MCP tool call.

    The LLM never supplies the workflow.
    Worker injects it automatically.
    """

    wrapped = []

    for tool in tools:
        current_tool = tool

        async def _call(
            _tool=current_tool,
            **kwargs,
        ):
            kwargs.pop("workflow", None)

            return await _tool.ainvoke(
                {
                    **kwargs,
                    "workflow": workflow_json,
                }
            )

        wrapped.append(
            StructuredTool.from_function(
                coroutine=_call,
                name=current_tool.name,
                description=current_tool.description,
                args_schema=current_tool.args_schema,
            )
        )

    return wrapped


COORDINATOR_SKILLS_FILE = str(Path(__file__).resolve().parents[1] / "coordinator" / "skills.md")


async def worker_node(state: GithubAgentState) -> GithubAgentState:
    """
    Worker LangGraph node.
    """

    dna = get_dna(
        worker_name(),
        SKILLS_FILE,
    )

    if dna is None:
        raise RuntimeError("AgentDNA not defined for worker_node")

    coordinator_dna = get_dna(coordinator_name(), COORDINATOR_SKILLS_FILE)

    if coordinator_dna is None:
        raise RuntimeError("Coordinator AgentDNA not found")

    handle_result = verify_inbound(
        dna,
        state,
    )

    if handle_result is None:
        raise RuntimeError("worker_node: failed to deserialize or verify workflow")

    #
    # Verification failed
    #
    if not handle_result.verification.valid:
        latest_envelope = get_latest_envelope(handle_result.workflow)

        rejected_workflow = dna.build(
            recipient_actor_id=latest_envelope.from_.id,
            recipient_actor_name=latest_envelope.from_.name,
            recipient_actor_type=latest_envelope.from_.type,
            payload=json.dumps(
                {
                    "status": "rejected",
                    "reason": "verification_failed",
                }
            ),
            verification_result=handle_result.verification,
            workflow=handle_result.workflow,
        )

        return {
            "_agentdna_terminal": True,
            "_agentdna_terminal_reason": "verification_failed",
            "_agentdna_workflow": serialize_workflow(rejected_workflow),
            "final_response": "Request rejected due to workflow verification failure.",
        }

    task_spec = state.get(
        "task_spec",
        "",
    )

    if not task_spec:
        return {
            "final_response": "Worker received an empty task spec.",
            "worker_messages": [],
        }

    workflow_json = serialize_workflow(handle_result.workflow)

    raw_tools = await get_github_tools()

    tools = _wrap_tools(
        raw_tools,
        workflow_json,
    )

    llm = make_llm(
        temperature=0.0,
    )

    agent = create_react_agent(
        llm,
        tools,
        prompt=WORKER_SYSTEM,
    )

    result = await agent.ainvoke({"messages": [HumanMessage(content=task_spec)]})

    messages = result.get(
        "messages",
        [],
    )

    final = messages[-1] if messages else None

    final_text = getattr(final, "content", "") if final is not None else ""

    if not isinstance(
        final_text,
        str,
    ):
        final_text = str(final_text)

    returned_workflow_json = _extract_workflow(messages)

    if not returned_workflow_json:
        failed_workflow = dna.build(
            recipient_actor_id=coordinator_dna.get_actor_id(),
            recipient_actor_name=coordinator_dna.name,
            recipient_actor_type=coordinator_dna.type,
            payload=json.dumps(
                {
                    "status": "failed",
                    "reason": "mcp_did_not_return_workflow",
                }
            ),
            workflow=handle_result.workflow,
        )

        return {
            "_agentdna_terminal": True,
            "_agentdna_terminal_reason": "mcp_failed",
            "_agentdna_workflow": serialize_workflow(failed_workflow),
            "final_response": "MCP did not return a workflow.",
        }

    returned_workflow = deserialize_workflow(returned_workflow_json)

    worker_to_coordinator_workflow = dna.build(
        recipient_actor_id=coordinator_dna.get_actor_id(),
        recipient_actor_name=coordinator_dna.name,
        recipient_actor_type=coordinator_dna.type,
        payload=json.dumps(
            {
                "agent": worker_name(),
                "status": "completed",
                "final_response_preview": final_text[:500],
            }
        ),
        workflow=returned_workflow,
    )

    return {
        "worker_messages": messages,
        "final_response": final_text,
        "_agentdna_workflow": serialize_workflow(worker_to_coordinator_workflow),
        "_agentdna_phase": "finalize",
    }
