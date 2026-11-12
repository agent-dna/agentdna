"""
The gate service: the AgentDNA checks for a gateway that can't run them itself
(Azure APIM, Apigee, AWS API Gateway, Kong, Envoy). The gateway keeps routing
and forwarding; it sends each call here first and does what the answer says.

    app = create_app(gateway, cbac_fn)        # run with any ASGI server, e.g. uvicorn

1. Before forwarding, the gateway sends:

    POST /check   {"path": "/github/repos/o/r/issues?x=1", "body": "<the raw request body>",
                   "headers": {<what the caller sent>}}                    # headers: for the credential row

and gets back a yes:

    {"allowed": true, "method": "POST", "path": "...", "content_type": "...", "body": "...", "encoding": "",
     "workflow": "...", "run_id": "..."}

Forward method, path, content_type and body to the app, never the request's
own, and keep workflow (it ends in the gate's signed "allowed"). If encoding is
"base64", the body is binary: decode it before forwarding. Or a no:

    {"allowed": false, "status": 403, "reason": "...", "code": 1001}

Answer the caller with that status. The refusal is already signed and recorded.

2. When the app answers, whatever the answer, the gateway sends:

    POST /answered   {"workflow": "<the workflow from /check>", "status": 201, "content_type": "...", "body": "...",
                      "encoding": "base64", "headers": {<what the gateway sent the app>},
                      "destination": "<the app's URL>"}

encoding: send "base64" with the body base64-encoded, so binary answers (a PDF)
arrive intact, or leave it out for a text body.

On success the gate signs the app's answer onto its "allowed" and returns
{"body": {...}}: send exactly that to the caller. On an error the gate signs
and records it, and returns {"recorded": true}: pass the app's answer through.
If the gateway itself stops the call after the gate's yes (no key, say), it
sends {"workflow", "status": 403, "reason": "..."} and the reason is recorded.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urljoin

import requests
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentdna.auth.httpobserver import SOURCE_SERVER_OUT, record_call
from agentdna.error import (
    ADMIN_WHITELIST_CHECK_FAILED,
    ADMIN_WHITELIST_CHECK_SERVER_ERROR,
    MIDDLEWARE_EXECUTION_FAILED,
    RESULT_OK,
    TOOL_EXECUTION_FAILED,
)
from agentdna.http.request import RequestRefused, SignedRequest, record_failure
from agentdna.http.wire import (
    body_from_text,
    body_to_text,
    build_http_answer_payload,
    workflow_from_body,
    workflow_to_body,
)
from agentdna.mcp.server.observer import SOURCE_SERVER_IN
from agentdna.mcp.server.types import CbacFn
from agentdna.types import VERIFY_HEAVY, VERIFY_LIGHT, load_workflow


def create_app(gateway, cbac_fn: CbacFn | None = None) -> Starlette:
    """The gate service. gateway is the gateway's own AgentDNA identity."""

    async def check_endpoint(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse(
            await check(gateway, data["path"], data["body"], cbac_fn, data.get("headers"))
        )

    async def answered_endpoint(request: Request) -> JSONResponse:
        data = await request.json()
        return JSONResponse(
            await answered(
                gateway,
                data["workflow"],
                data["status"],
                data.get("content_type", ""),
                data.get("body", ""),
                data.get("headers"),
                data.get("destination", ""),
                data.get("reason", ""),
                data.get("encoding", ""),
            )
        )

    return Starlette(
        routes=[
            Route("/check", check_endpoint, methods=["POST"]),
            Route("/answered", answered_endpoint, methods=["POST"]),
        ]
    )


async def check(
    gateway, path: str, body: str, cbac_fn: CbacFn | None = None, headers: dict | None = None
) -> dict:
    """
    Run every check on one call. The first no stops it.
    headers: what the caller sent the gateway, for the credential row (server_in).
    """

    # Credential row: which key arrived with this call. The gate doesn't judge
    # the key, so its status is unknown, as at an MCP server.
    opened = workflow_from_body(body)
    if opened and headers is not None:
        await asyncio.to_thread(
            record_call,
            gateway,
            opened[0].get_latest_envelope(),
            SOURCE_SERVER_IN,
            headers,
            gateway.get_actor_id(),
        )

    # Open, GRADE (every signature, back to the person), match (signed for this path?).
    # SignedRequest signs and records its own refusals (a forged workflow only logged).
    try:
        signed_request = await asyncio.to_thread(SignedRequest, gateway, body, path, VERIFY_HEAVY)
    except RequestRefused as refused:
        status = 400 if refused.code == 0 else 403  # 0: no signed call at all
        return {"allowed": False, "status": status, "reason": refused.reason, "code": refused.code}

    workflow = signed_request.workflow
    agent_id = workflow.get_latest_envelope().from_

    # Whitelist: an admin approved everyone who signed the workflow (as the MCP server checks)
    try:
        refused = await asyncio.to_thread(
            _not_whitelisted, gateway.agentdna_admin_url, workflow.list_actors()
        )
    except Exception as error:
        return await _refuse(
            gateway,
            workflow,
            f"could not check the whitelist: {error}",
            ADMIN_WHITELIST_CHECK_SERVER_ERROR,
        )
    if refused:
        return await _refuse(
            gateway,
            workflow,
            f"not whitelisted: {', '.join(refused)}",
            ADMIN_WHITELIST_CHECK_FAILED,
        )

    # CBAC: the call fits what the person asked (the same interface as the MCP server)
    if cbac_fn:
        try:
            decision, status, message_hash = await cbac_fn(
                agent_id,
                gateway.get_actor_id(),
                f"{signed_request.method} {signed_request.path}",
                _arguments(signed_request.body),
                _intent(workflow),
                "",
                workflow.id,
            )
        except Exception as error:
            return await _refuse(
                gateway, workflow, f"could not run CBAC: {error}", MIDDLEWARE_EXECUTION_FAILED
            )
        if decision.lower() != "allow":
            return await _refuse(
                gateway, workflow, f"CBAC said {decision}", status, record_payload=message_hash
            )

    # Yes: sign it, and say exactly what to forward
    allowed = await asyncio.to_thread(
        gateway.build,
        payload=json.dumps({"allowed": f"{signed_request.method} {signed_request.path}"}),
        previous_workflows=workflow,
    )
    body, encoding = body_to_text(signed_request.body)
    return {
        "allowed": True,
        "method": signed_request.method,
        "path": signed_request.path,
        "content_type": signed_request.content_type,
        "body": body,
        "encoding": encoding,
        "workflow": allowed.serialize(),
        "run_id": workflow.get_root_envelope().run_id,  # the person's run: a gateway can fetch keys for it
    }


async def answered(
    gateway,
    workflow: str,
    status: int,
    content_type: str = "",
    body: str = "",
    headers: dict | None = None,
    destination: str = "",
    reason: str = "",
    encoding: str = "",
) -> dict:
    """
    The app answered. On success the app's answer is signed onto the gate's
    "allowed", and the body to send back is returned. On an error, the error is
    signed and recorded. Only the gate's own, genuine "allowed" is accepted, so
    nobody else can get an answer signed.

    headers and destination: what the gateway sent the app, and where, for the
    credential row (server_out). reason: why the call failed, when the gateway
    itself stopped it after the gate's yes (no key for the app, say).
    encoding: "base64" if the gateway sent the body base64-encoded.
    """
    try:
        allowed = load_workflow(json.loads(workflow))
        tip = allowed.get_latest_envelope()
        genuine = tip.from_ == gateway.get_actor_id() and "allowed" in json.loads(tip.payload)
    except Exception:
        genuine = False
    if not genuine or await asyncio.to_thread(gateway.verify, allowed, VERIFY_LIGHT) != RESULT_OK:
        return {"error": 'not an "allowed" this gateway signed'}

    # Credential row: which key went to the app, and what the app said about it.
    # Filed under the agent's call, like the rows before it.
    if headers is not None:
        agent_call = (allowed.get_latest_envelope().parent_envelope or [])[0]
        await asyncio.to_thread(
            record_call, gateway, agent_call, SOURCE_SERVER_OUT, headers, destination, status
        )

    if status >= 400:
        await asyncio.to_thread(
            record_failure,
            gateway,
            allowed,
            reason or f"the app answered {status}",
            TOOL_EXECUTION_FAILED,
        )
        return {"recorded": True}

    # Text is signed as text, a binary answer (a PDF) as base64
    body, encoding = body_to_text(body_from_text(body, encoding))
    answer = await asyncio.to_thread(
        gateway.build,
        payload=build_http_answer_payload(body, content_type, status, encoding),
        previous_workflows=allowed,
    )
    return {"body": workflow_to_body(answer)}


async def _refuse(
    gateway, workflow, reason: str, code: int, record_payload: str | None = None
) -> dict:
    """Sign the refusal onto the workflow and record it. CBAC's reason may hold
    personal data, so only its hash is recorded, as the MCP server does."""
    await asyncio.to_thread(record_failure, gateway, workflow, record_payload or reason, code)
    return {"allowed": False, "status": 403, "reason": reason, "code": code}


def _not_whitelisted(admin_url: str, actor_ids: list[str]) -> list[str]:
    """The actors the admin server has not whitelisted: the same request as the MCP server's."""
    response = requests.post(
        urljoin(admin_url, "/agent-admin/v1/whitelist/check"), json={"dids": actor_ids}, timeout=30
    )
    response.raise_for_status()
    answer = response.json()
    if not answer["status"]:
        raise RuntimeError("the admin server could not check the whitelist")
    return answer["data"]["blacklist"]


def _intent(workflow) -> str:
    """What the person asked: the body of their signed call, or their plain text."""
    payload = workflow.get_root_envelope().payload
    try:
        return json.loads(payload)["body"]
    except Exception:
        return payload


def _arguments(body: str | bytes) -> dict:
    """The signed body as CBAC's tool arguments: the JSON object, the text as is, or a binary body's size."""
    if isinstance(body, bytes):
        return {"body": f"<binary, {len(body)} bytes>"}
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {"body": body}
