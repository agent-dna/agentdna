"""
GitHub MCP server — exposes two tools the Worker agent can call:
  - create_issue
  - create_pr

Phase 2: each GitHub HTTP call is wrapped with ``cbac.authorize_agent_app_interaction(...)``
so the CBAC layer evaluates the Worker's authorization before the request
leaves the MCP server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import httpx
from fastmcp import FastMCP

from app.agent_names import worker_name
from app.config import settings
from app.integrations.agentdna import agentdna_registry, is_agentdna_enabled
from app.utils import deserialize_workflow, serialize_workflow
from agentdna.types import VerificationResult, Issue
from agentdna.helpers import get_envelope_depth
from agentdna.types import IntentWorkflow, Actor

mcp = FastMCP("github-mcp")


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


def _worker_skills_file() -> str:
    """Absolute path to the Worker agent's skills.md."""
    return str(
        Path(__file__).resolve().parents[1] / "agents" / "worker" / "skills.md"
    )


def _worker_did() -> Optional[str]:
    """
    Look up the Worker agent's DID from this process's AgentDNA registry.
    """
    dna = agentdna_registry.get(worker_name(), policy_file=_worker_skills_file())
    return dna.get_actor_id() if dna else None

def _worker_dna():
    return agentdna_registry.get(
        worker_name(),
        policy_file=_worker_skills_file(),
    )

def _post_via_cbac(
    url: str,
    body: dict,
    agent_id: str,
    action_intent: str,
    workflow: IntentWorkflow,
):
    """
    Run a GitHub request through CBAC.

    Returns:
        (
            decision: str,
            response: requests.Response | None,
            detail: str | None,
        )
    """

    from agentdna.cbac import CBAC
    from agentdna.provenance import Provenance

    cbac = CBAC(
        provenance=Provenance(
            name="cbac-internal",
            api_key=os.environ.get(
                "AGENTDNA_API_KEY",
                ""
            ),
            provenance_url=os.environ.get(
                "AGENTDNA_CHAIN_URL",
                "",
            ),
        ),
        cbac_url=os.environ.get(
            "AGENTDNA_CBAC_URL",
            "",
        ),
    )

    try:
        response = cbac.authorise_agent_app_interaction(
            agent_id=agent_id,
            action_intent=action_intent,
            envelope=workflow,
            app_url=url,
            app_method="POST",
            app_headers=_gh_headers(),
            app_body=body,
            app_timeout=float(
                os.environ.get(
                    "CBAC_TIMEOUT_SECONDS",
                    "3600",
                )
            ),
        )

    except PermissionError as exc:
        return (
            "deny",
            None,
            str(exc),
        )

    except RuntimeError as exc:
        return (
            "error",
            None,
            str(exc),
        )

    except Exception as exc:
        print(
            f"[cbac] authorise_agent_app_interaction raised "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        return (
            "error",
            None,
            f"{type(exc).__name__}: {exc}",
        )

    decision = response.headers.get(
        "X-CBAC-Decision",
        "allow",
    )

    return (
        decision,
        response,
        None,
    )

def _cbac_block(decision: str, request: str, response: Optional[str]) -> dict:
    """
    Build the CBAC attestation recorded on the Worker's response envelope.

    ``request`` is the target GitHub API URL of the action; ``response`` is the
    stringified REST response GitHub returned, or ``None`` when CBAC blocked the
    call before it ever reached GitHub.
    """
    return {
        "decision": decision,
        "app": "github",
        "request": request,
        "response": response,
    }


async def _post_direct(url: str, body: dict) -> httpx.Response:
    """Direct GitHub call when AgentDNA/CBAC is disabled."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.post(url, json=body, headers=_gh_headers())


@mcp.tool()
async def create_issue(
    repo: str,
    title: str,
    body: str,
    workflow: str = "",
) -> dict:
    """
    Create a GitHub issue.

    Args:
        repo: Repository in owner/name format.
        title: Issue title.
        body: Issue body.
        workflow: Serialized IntentWorkflow injected by Worker.

    Returns:
        {
            "status": "...",
            "workflow": "...",
            ...
        }
    """
    url = f"{settings.github_api_url}/repos/{repo}/issues"

    request_payload = {
        "title": title,
        "body": body,
    }

    worker_dna = _worker_dna()

    intent_workflow = None

    if workflow and worker_dna:
        intent_workflow = deserialize_workflow(
            workflow
        )

        intent_workflow = worker_dna.build(
            recipient_actor_id="",
            recipient_actor_name="github",
            recipient_actor_type="app",
            payload=json.dumps({
                "action": "create_issue",
                "repo": repo,
                "title": title,
                "body_preview": body[:200],
            }),
            workflow=intent_workflow,
            from_actor=Actor(
                id="",
                name="github",
                type="app"
            )
        )

    agent_id = _worker_did()
    if agent_id == "":
        raise ValueError(
            "AgentDNA is enabled but Worker agent has no DID"
        )

    try:
        if agent_id and is_agentdna_enabled() and intent_workflow:
            decision, response, detail = _post_via_cbac(
                url=url,
                body=request_payload,
                agent_id=agent_id,
                action_intent=f"github:create_issue:{repo}",
                workflow=intent_workflow,
            )


            if response is None:
                if intent_workflow and worker_dna:
                    depth = get_envelope_depth(
                        intent_workflow.envelope
                    )

                    intent_workflow = worker_dna.build(
                        recipient_actor_id=worker_dna.get_actor_id(),
                        recipient_actor_name=worker_dna.name,
                        recipient_actor_type=worker_dna.type,
                        payload=json.dumps({
                            "status": "denied",
                            "reason": detail,
                        }),
                        verification_result=VerificationResult(
                            valid=False,
                            chain_depth=depth + 1,
                            issues=[Issue(
                                depth=depth + 1,
                                reason=f"cbac denied: {detail}"
                            )]
                        ),
                        workflow=intent_workflow,
                        from_actor=Actor(
                            id="",
                            name="github",
                            type="app"
                        )
                    )

                status = (
                    "denied"
                    if decision == "deny"
                    else "error"
                )

                return {
                    "status": status,
                    "error": detail,
                    "workflow": (
                        serialize_workflow(intent_workflow)
                        if intent_workflow
                        else ""
                    ),
                }

            status_code = response.status_code
            text = response.text

            try:
                data = response.json()
            except Exception:
                data = {}

        #
        # Direct GitHub path
        #
        else:
            response = await _post_direct(
                url,
                request_payload,
            )
            status_code = response.status_code
            text = response.text
            data = (
                response.json()
                if status_code < 300
                else {}
            )

    except Exception as exc:
        if worker_dna and intent_workflow:
            depth = get_envelope_depth(
                intent_workflow.envelope
            )

            intent_workflow = worker_dna.build(
                recipient_actor_id=worker_dna.get_actor_id(),
                recipient_actor_name=worker_dna.name,
                recipient_actor_type=worker_dna.type,
                payload=json.dumps({
                    "status": "error",
                }),
                verification_result=VerificationResult(
                    valid=False,
                    chain_depth=depth + 1,
                    issues=[
                        Issue(
                            depth=depth + 1,
                            reason=str(exc),
                        )
                    ]
                ),
                workflow=intent_workflow,
                from_actor=Actor(
                    id="",
                    name="github",
                    type="app"
                )
            )

        return {
            "status": "error",
            "error": str(exc),
            "workflow": (
                serialize_workflow(intent_workflow)
                if intent_workflow
                else ""
            ),
        }

    if status_code >= 300:
        if intent_workflow and worker_dna:
            depth = get_envelope_depth(
                intent_workflow.envelope
            )

            intent_workflow = worker_dna.build(
                recipient_actor_id=worker_dna.get_actor_id(),
                recipient_actor_name=worker_dna.name,
                recipient_actor_type=worker_dna.type,
                payload=json.dumps({
                    "status": "failed",
                    "http_status": status_code,
                }),
                verification_result=VerificationResult(
                    valid=False,
                    chain_depth=depth + 1,
                    issues=[
                        Issue(
                            depth=depth + 1,
                            reason=text[:500],
                        )
                    ]
                ),
                workflow=intent_workflow,
                from_actor=Actor(
                    id="",
                    name="github",
                    type="app"
                )
            )

        return {
            "status": "failed",
            "http_status": status_code,
            "error": text,
            "workflow": (
                serialize_workflow(intent_workflow)
                if intent_workflow
                else ""
            ),
        }


    if intent_workflow and worker_dna:
        intent_workflow = worker_dna.build(
            recipient_actor_id=worker_dna.get_actor_id(),
            recipient_actor_name=worker_dna.name,
            recipient_actor_type=worker_dna.type,
            payload=json.dumps({
                "status": "created",
                "issue_number": data.get("number"),
                "html_url": data.get("html_url"),
                "title": data.get("title"),
            }),
            workflow=intent_workflow,
            from_actor=Actor(
                id="",
                name="github",
                type="app"
            )
        )

    return {
        "status": "created",
        "issue_number": data.get("number"),
        "html_url": data.get("html_url"),
        "title": data.get("title"),
        "workflow": (
            serialize_workflow(intent_workflow)
            if intent_workflow
            else ""
        ),
    }

@mcp.tool()
async def create_pr(
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
    workflow: str = "",
) -> dict:
    """
    Create a GitHub pull request.

    Args:
        repo: Repository in owner/name format.
        title: Pull request title.
        body: Pull request body.
        head: Source branch.
        base: Target branch.
        workflow: Serialized IntentWorkflow injected by Worker.
    """

    url = f"{settings.github_api_url}/repos/{repo}/pulls"

    request_payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }

    worker_dna = _worker_dna()

    intent_workflow = None

    if workflow and worker_dna:
        intent_workflow = deserialize_workflow(workflow)

        intent_workflow = worker_dna.build(
            recipient_actor_id="",
            recipient_actor_name="github",
            recipient_actor_type="app",
            payload=json.dumps(
                {
                    "action": "create_pr",
                    "repo": repo,
                    "title": title,
                    "head": head,
                    "base": base,
                    "body_preview": body[:200],
                }
            ),
            workflow=intent_workflow,
        )

    agent_id = _worker_did()

    if agent_id == "":
        raise ValueError(
            "AgentDNA is enabled but Worker agent has no DID"
        )

    try:

        if agent_id and is_agentdna_enabled() and intent_workflow:

            decision, response, detail = _post_via_cbac(
                url=url,
                body=request_payload,
                agent_id=agent_id,
                action_intent=f"github:create_pr:{repo}",
                workflow=intent_workflow,
            )

            if response is None:

                if intent_workflow and worker_dna:

                    depth = get_envelope_depth(
                        intent_workflow.envelope
                    )

                    intent_workflow = worker_dna.build(
                        recipient_actor_id=worker_dna.get_actor_id(),
                        recipient_actor_name=worker_dna.name,
                        recipient_actor_type=worker_dna.type,
                        payload=json.dumps(
                            {
                                "status": "denied",
                                "reason": detail,
                            }
                        ),
                        verification_result=VerificationResult(
                            valid=False,
                            chain_depth=depth + 1,
                            issues=[
                                Issue(
                                    depth=depth + 1,
                                    reason=f"cbac denied: {detail}",
                                )
                            ],
                        ),
                        workflow=intent_workflow,
                    )

                status = (
                    "denied"
                    if decision == "deny"
                    else "error"
                )

                return {
                    "status": status,
                    "error": detail,
                    "workflow": (
                        serialize_workflow(intent_workflow)
                        if intent_workflow
                        else ""
                    ),
                }

            status_code = response.status_code
            text = response.text

            try:
                data = response.json()
            except Exception:
                data = {}

        else:

            response = await _post_direct(
                url,
                request_payload,
            )

            status_code = response.status_code
            text = response.text

            data = (
                response.json()
                if status_code < 300
                else {}
            )

    except Exception as exc:

        if worker_dna and intent_workflow:

            depth = get_envelope_depth(
                intent_workflow.envelope
            )

            intent_workflow = worker_dna.build(
                recipient_actor_id=worker_dna.get_actor_id(),
                recipient_actor_name=worker_dna.name,
                recipient_actor_type=worker_dna.type,
                payload=json.dumps(
                    {
                        "status": "error",
                    }
                ),
                verification_result=VerificationResult(
                    valid=False,
                    chain_depth=depth + 1,
                    issues=[
                        Issue(
                            depth=depth + 1,
                            reason=str(exc),
                        )
                    ],
                ),
                workflow=intent_workflow,
            )

        return {
            "status": "error",
            "error": str(exc),
            "workflow": (
                serialize_workflow(intent_workflow)
                if intent_workflow
                else ""
            ),
        }

    if status_code >= 300:

        if worker_dna and intent_workflow:

            depth = get_envelope_depth(
                intent_workflow.envelope
            )

            intent_workflow = worker_dna.build(
                recipient_actor_id=worker_dna.get_actor_id(),
                recipient_actor_name=worker_dna.name,
                recipient_actor_type=worker_dna.type,
                payload=json.dumps(
                    {
                        "status": "failed",
                        "http_status": status_code,
                    }
                ),
                verification_result=VerificationResult(
                    valid=False,
                    chain_depth=depth + 1,
                    issues=[
                        Issue(
                            depth=depth + 1,
                            reason=text[:500],
                        )
                    ],
                ),
                workflow=intent_workflow,
            )

        return {
            "status": "failed",
            "http_status": status_code,
            "error": text,
            "workflow": (
                serialize_workflow(intent_workflow)
                if intent_workflow
                else ""
            ),
        }

    if worker_dna and intent_workflow:

        intent_workflow = worker_dna.build(
            recipient_actor_id=worker_dna.get_actor_id(),
            recipient_actor_name=worker_dna.name,
            recipient_actor_type=worker_dna.type,
            payload=json.dumps(
                {
                    "status": "created",
                    "pr_number": data.get("number"),
                    "html_url": data.get("html_url"),
                    "title": data.get("title"),
                    "head": head,
                    "base": base,
                }
            ),
            workflow=intent_workflow,
        )

    return {
        "status": "created",
        "pr_number": data.get("number"),
        "html_url": data.get("html_url"),
        "title": data.get("title"),
        "workflow": (
            serialize_workflow(intent_workflow)
            if intent_workflow
            else ""
        ),
    }

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    )
