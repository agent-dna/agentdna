"""
Coordinator agent — parses the user's free-text request into a clear,
actionable task spec for the Worker agent.

AgentDNA integration:
  - Verifies the inbound envelope (the user's signed intent) before doing work.
  - Signs a forwarding envelope committing the Coordinator's output to the
    upstream chain so the Worker can verify on receipt.
"""
from __future__ import annotations

from pathlib import Path
import json
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent_names import coordinator_name, worker_name
from app.agents.agentdna_helpers import get_dna, verify_inbound
from app.config import settings
from app.llm import make_llm
from app.utils import serialize_workflow
from agentdna.helpers import get_latest_envelope

COORDINATOR_SKILLS_FILE = str(Path(__file__).parent / "skills.md")

COORDINATOR_SYSTEM = """You are the Coordinator agent in a two-agent system.

Your job:
1. Read the user's request about a GitHub task.
2. Identify which action they want — either `create_issue` or `create_pr`.
3. Extract the required fields.
4. Produce a clear, structured task description for the Worker agent.

The Worker has these GitHub tools available:
- create_issue(repo, title, body)
- create_pr(repo, title, body, head, base)

Field requirements:
- `repo`: in "owner/name" format.
- For issues: title, body.
- For PRs: title, body, head branch, base branch (default "main" if unspecified).

If a required detail is missing, make a reasonable assumption and explicitly note it.
If the request is ambiguous about whether it's an issue or a PR, prefer the more
likely intent and state your reasoning.

Output a concise plain-text instruction the Worker can act on directly.
Do NOT call any tools yourself."""
from app.agents.state import GithubAgentState

async def coordinator_node(state: GithubAgentState) -> GithubAgentState:
    """LangGraph node — verifies inbound envelope, produces task_spec, signs forward."""
    user_did = state.get("_agentdna_user_id")
    dna = get_dna(coordinator_name(), COORDINATOR_SKILLS_FILE)
    next_agent_dna = get_dna(worker_name())
    
    if dna is None:
        raise RuntimeError("AgentDNA not defined for coordinator_node")
    if next_agent_dna is None:
        raise RuntimeError("AgentDNA not defined for worker_node")
    
    handle_result = verify_inbound(dna, state)
    if handle_result is None:
        raise RuntimeError(
            "coordinator_node: failed to deserialize or verify workflow"
        )

    latest_envelope = get_latest_envelope(
        handle_result.workflow
    )
    if (
        latest_envelope.from_.type == next_agent_dna.type
        and latest_envelope.from_.id == next_agent_dna.get_actor_id()
    ):
        root = latest_envelope
        while root.parent_envelope is not None:
            root = root.parent_envelope

        final_workflow = dna.build(
            recipient_actor_id=root.from_.id,
            recipient_actor_name=root.from_.name,
            recipient_actor_type=root.from_.type,
            payload=json.dumps(
                {
                    "agent": coordinator_name(),
                    "status": "completed",
                    "final_response_preview": state.get(
                        "final_response",
                        "",
                    )[:500],
                }
            ),
            workflow=handle_result.workflow,
        )

        return {
            "final_response": state.get(
                "final_response",
                "",
            ),
            "worker_messages": state.get(
                "worker_messages",
                [],
            ),
            "_agentdna_user_id": state.get(
                "_agentdna_user_id",
                "",
            ),
            "_agentdna_workflow": serialize_workflow(
                final_workflow
            ),
        }

    if not handle_result.verification.valid:
        latest_envelope = get_latest_envelope(handle_result.workflow)

        rejected_workflow = dna.build(
            recipient_actor_id=latest_envelope.from_.id,
            recipient_actor_name=latest_envelope.from_.name,
            recipient_actor_type=latest_envelope.from_.type,
            payload=json.dumps({
                "status": "rejected",
                "reason": "verification_failed",
            }),
            verification_result=handle_result.verification,
            workflow=handle_result.workflow,
        )

        return {
            "_agentdna_user_id": state.get("_agentdna_user_id", ""),
            "_agentdna_terminal": True,
            "_agentdna_terminal_reason": "verification_failed",
            "_agentdna_workflow": serialize_workflow(rejected_workflow),
        }

    user_input = state.get("user_input", "")

    llm = make_llm(settings.gemini_temperature)
    response = await llm.ainvoke(
        [
            SystemMessage(content=COORDINATOR_SYSTEM),
            HumanMessage(content=user_input),
        ]
    )
    task_spec = (
        response.content if isinstance(response.content, str) else str(response.content)
    )

    coordinator_workflow = dna.build(
        recipient_actor_id=next_agent_dna.get_actor_id(),
        recipient_actor_name=next_agent_dna.name,
        recipient_actor_type=next_agent_dna.type,
        payload=json.dumps({
            "agent":             coordinator_name(),
            "action":            "produce_task_spec",
            "task_spec_preview": task_spec[:200],
        }),
        workflow=handle_result.workflow
    )

    coordinator_workflow_str = serialize_workflow(coordinator_workflow)

    update: GithubAgentState = {
        "task_spec": task_spec,
        "_agentdna_phase": "execute",
        "_agentdna_user_id": state.get("_agentdna_user_id", "")
    }
    if coordinator_workflow_str != "":
        update["_agentdna_workflow"] = coordinator_workflow_str
    return update
