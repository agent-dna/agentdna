"""
Guarded Worker — same role as app/agents/worker, but GitHub tools are
served behind MCP and governed *from the client* by `cbac_intercept`.

This one file plays two roles:

  - Imported by the LangGraph flow: exposes `worker_node`, which fetches
    the tools remotely (over MCP) and calls them inside a governance
    scope.
  - Run standalone (`python -m app.agents.worker_guarded`): hosts the
    MCP server the worker connects to.

The MCP server here is deliberately un-governed — a stand-in for a
third-party server you don't own and can't decorate. All CBAC
enforcement happens on the client: `cbac_intercept` authorizes each
outgoing tool call against the worker's policy before it leaves the
process, so a denied call never reaches the server. The interceptor
reads the ambient `cbac_context` that `worker_node` opens.

(`@cbac_guard` governs an agent's *own* in-process tools; an MCP tool
call crosses a process boundary into someone else's server, so it is
gated at the client edge instead.)

Delegation (coordinator <-> worker) keeps the original build()/handle()
protocol; only tool calls go through the guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from app.agent_names import coordinator_name, worker_name
from app.agents.agentdna_helpers import get_dna, verify_inbound
from app.agents.state import GithubAgentState
from app.agents.worker.agent import WORKER_SYSTEM
from app.config import settings
from app.llm import make_llm
from app.utils import serialize_workflow as serialize_state_workflow
from fastmcp import FastMCP
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from agentdna.cbac import cbac_context, cbac_guard
from agentdna.cbac.mcp import cbac_intercept
from agentdna.helpers import get_latest_envelope, get_root_envelope

SKILLS_FILE = str(Path(__file__).parent / "worker" / "skills.md")
COORDINATOR_SKILLS_FILE = str(Path(__file__).parent / "coordinator" / "skills.md")


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


# ── MCP server side: plain tools, no CBAC awareness (governed client-side) ───

mcp = FastMCP("github-mcp-guarded")


async def _github_post(url: str, body: dict) -> dict:
    """POST to GitHub and shape the tool result. Runs only after the
    client-side interceptor has authorized the call."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=_gh_headers(), json=body)
    if response.status_code >= 300:
        return {"status": "failed", "http_status": response.status_code, "error": response.text}
    return {"status": "created", **response.json()}


@mcp.tool()
async def create_issue(repo: str, title: str, body: str) -> dict:
    """Create a GitHub issue.

    Args:
        repo: Repository in owner/name format.
        title: Issue title.
        body: Issue body.
    """
    return await _github_post(
        f"{settings.github_api_url}/repos/{repo}/issues",
        {"title": title, "body": body},
    )


@mcp.tool()
async def create_pr(repo: str, title: str, body: str, head: str, base: str = "main") -> dict:
    """Create a GitHub pull request.

    Args:
        repo: Repository in owner/name format.
        title: Pull request title.
        body: Pull request body.
        head: Source branch.
        base: Target branch.
    """
    return await _github_post(
        f"{settings.github_api_url}/repos/{repo}/pulls",
        {"title": title, "body": body, "head": head, "base": base},
    )


# ── Worker (client) side: fetch the guarded tools over MCP ───────────────────


async def _get_guarded_tools() -> list:
    timeout_s = float("103600")
    client = MultiServerMCPClient(
        {
            "github": {
                "url": settings.mcp_server_url,
                "transport": "streamable_http",
                "timeout": timeout_s,
                "sse_read_timeout": timeout_s,
            }
        },
        tool_interceptors=[cbac_intercept],
    )
    return await client.get_tools()

 
# An in-process tool: runs in THIS process, not over MCP. Governed by the same
# cbac_context as the MCP tools, but enforced by @cbac_guard (authorized
# in-process) rather than the client interceptor.
@cbac_guard(action_intent=lambda kw: f"repo:draft_summary:{kw['repo']}")
async def draft_summary(repo: str, notes: str) -> dict:
    """Condense raw notes into a short change summary for a repo.

    Args:
        repo: Repository in owner/name format.x
        notes: Raw notes to condense.
    """
    return {"status": "drafted", "repo": repo, "summary": " ".join(notes.split())[:280]}


# ── Worker node ───────────────────────────────────────────────────────────────


async def worker_node(state: GithubAgentState) -> GithubAgentState:
    dna = get_dna(worker_name(), SKILLS_FILE)
    if dna is None:
        raise RuntimeError("AgentDNA not defined for worker_node")

    coordinator_dna = get_dna(coordinator_name(), COORDINATOR_SKILLS_FILE)
    if coordinator_dna is None:
        raise RuntimeError("Coordinator AgentDNA not found")

    handle_result = verify_inbound(dna, state)
    if handle_result is None:
        raise RuntimeError("worker_node: failed to deserialize or verify workflow")

    if not handle_result.verification.valid:
        latest_envelope = get_latest_envelope(handle_result.workflow)
        rejected_workflow = dna.build(
            recipient_actor_id=latest_envelope.from_.id,
            recipient_actor_name=latest_envelope.from_.name,
            recipient_actor_type=latest_envelope.from_.type,
            payload=json.dumps({"status": "rejected", "reason": "verification_failed"}),
            verification_result=handle_result.verification,
            workflow=handle_result.workflow,
        )
        return {
            "_agentdna_terminal": True,
            "_agentdna_terminal_reason": "verification_failed",
            "_agentdna_workflow": serialize_state_workflow(rejected_workflow),
            "final_response": "Request rejected due to workflow verification failure.",
        }

    task_spec = state.get("task_spec", "")
    if not task_spec:
        return {
            "final_response": "Worker received an empty task spec.",
            "worker_messages": [],
        }

    tools = [
        StructuredTool.from_function(coroutine=draft_summary),  # in-process, @cbac_guard
        *await _get_guarded_tools(),  # MCP, cbac_intercept
    ]
    agent = create_react_agent(make_llm(temperature=0.0), tools, prompt=WORKER_SYSTEM)

    root = get_root_envelope(handle_result.workflow)
    
    with cbac_context(agent_id=dna.get_actor_id(), user_intent=(root.payload if root else "")):
        result = await agent.ainvoke({"messages": [HumanMessage(content=task_spec)]})

    # The guard authorizes tool calls but no longer mutates the chain; the
    # delegation attestation continues from the inbound workflow.
    updated_workflow = handle_result.workflow

    messages = result.get("messages", [])
    final = messages[-1] if messages else None
    final_text = getattr(final, "content", "") if final is not None else ""
    if not isinstance(final_text, str):
        final_text = str(final_text)

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
        workflow=updated_workflow,
    )

    return {
        "worker_messages": messages,
        "final_response": final_text,
        "_agentdna_workflow": serialize_state_workflow(worker_to_coordinator_workflow),
        "_agentdna_phase": "finalize",
    }


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    )
