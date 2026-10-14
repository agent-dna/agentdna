"""IntentWorkflow propagation for one tools/call passing through the gateway.

Transport-neutral: works on parsed JSON-RPC messages, so the HTTP layer only
has to find the messages and hand them over.

For each call:

    request   build a workflow on the run's frontier, put it in params._meta
    response  extract the successor, verify it, advance the frontier

Fail-closed. A successful tool result is only returned to the agent once its
successor has been verified. When it cannot be, the result is replaced by a
JSON-RPC error, so the agent never acts on output that is missing from the
provenance chain. Error results pass through unchanged - they carry no output
to act on - but only advance the frontier when the server recorded them as a
tool execution failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentdna.error import RESULT_OK, TOOL_EXECUTION_FAILED
from agentdna.log import get_logger
from agentdna.mcp.context import MCPCallHandle
from agentdna.mcp.metadata import (
    AGENTDNA_META_KEY,
    workflow_from_metadata,
    workflow_to_metadata,
)
from agentdna.mcp.payload import build_mcp_tool_call_payload
from agentdna.types import IntentWorkflow

from .runs import Run

TOOLS_CALL = "tools/call"

# JSON-RPC reserves -32000 to -32099 for implementation-defined server errors.
AGENTDNA_GATEWAY_ERROR = -32050

logger = get_logger("agentdna.gateway")


class ToolCallRejected(Exception):
    """The gateway could not attach a workflow to a tools/call request."""


@dataclass
class PendingCall:
    """One tools/call that has been forwarded and not yet resolved."""

    run: Run
    handle: MCPCallHandle
    request_id: Any
    request_workflow: IntentWorkflow
    resolved: bool = False


def is_tool_call(message: object) -> bool:
    return (
        isinstance(message, Mapping)
        and message.get("method") == TOOLS_CALL
        and message.get("id") is not None
    )


def is_response_to(message: object, request_id: Any) -> bool:
    return (
        isinstance(message, Mapping)
        and message.get("id") == request_id
        and ("result" in message or "error" in message)
    )


def jsonrpc_error(request_id: Any, message: str, data: dict[str, Any] | None = None) -> dict:
    error: dict[str, Any] = {"code": AGENTDNA_GATEWAY_ERROR, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


async def begin_tool_call(run: Run, message: dict[str, Any]) -> PendingCall:
    """Sign a workflow for this call and attach it to `message` in place.

    Unrelated `_meta` fields and namespaces are preserved. AgentDNA owns
    `_meta.agentdna` and overwrites whatever a client put there.
    """

    params = message.get("params")
    if not isinstance(params, dict):
        raise ToolCallRejected("tools/call request has no params object")

    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ToolCallRejected("tools/call request has no tool name")

    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        raise ToolCallRejected("tools/call arguments must be an object")

    existing_meta = params.get("_meta")
    if existing_meta is not None and not isinstance(existing_meta, dict):
        raise ToolCallRejected("tools/call _meta must be an object")

    context = run.context
    handle = await context.begin_mcp_call()

    try:
        request_workflow = await asyncio.to_thread(
            context.dna.build,
            payload=build_mcp_tool_call_payload(name=name, arguments=arguments),
            previous_workflows=list(handle.batch.parent_frontier),
            verification_code=RESULT_OK,
        )
    except BaseException:
        await context.cancel_mcp_call(handle)
        raise

    if existing_meta and AGENTDNA_META_KEY in existing_meta:
        logger.warning(
            "gateway.tool_call.client_workflow_replaced",
            run_id=run.run_id,
            hint="the client attached its own AgentDNA workflow; do not install a client "
            "adapter on an MCP client that talks to the gateway",
        )

    meta = dict(existing_meta or {})
    agentdna_meta = dict(meta.get(AGENTDNA_META_KEY) or {})
    agentdna_meta.update(workflow_to_metadata(request_workflow)[AGENTDNA_META_KEY])
    meta[AGENTDNA_META_KEY] = agentdna_meta
    params["_meta"] = meta

    return PendingCall(
        run=run,
        handle=handle,
        request_id=message["id"],
        request_workflow=request_workflow,
    )


async def abandon_tool_call(pending: PendingCall) -> None:
    """Resolve a call that will never see its response.

    Upstream unreachable, the stream ended early, the client went away. No
    successor is invented; other calls in the same batch are unaffected.
    Safe to call more than once, and after the call was resolved.
    """

    if pending.resolved:
        return
    pending.resolved = True
    await pending.run.context.cancel_mcp_call(pending.handle)


async def finish_tool_call(pending: PendingCall, response: dict[str, Any]) -> dict[str, Any]:
    """Process the upstream response and return what the agent should receive.

    Always resolves the call, whatever happens.
    """

    try:
        return await _finish(pending, response)
    finally:
        await abandon_tool_call(pending)  # no-op when _finish completed the call


async def _finish(pending: PendingCall, response: dict[str, Any]) -> dict[str, Any]:
    run = pending.run
    context = run.context

    if "error" in response:
        # The request failed at the protocol level. Nothing was executed.
        return response

    result = response.get("result")
    if not isinstance(result, dict):
        return _reject(pending, "upstream tools/call response has no result object")

    result_type = result.get("resultType")
    if result_type not in (None, "complete"):
        # Not the final answer (input required, a task, ...). The follow-up
        # tools/call is a call of its own and gets its own workflow.
        return response

    successor = workflow_from_metadata(result.get("_meta"))
    is_error = bool(result.get("isError", False))

    if is_error:
        # No successor: an AgentDNA check on the server refused the call
        # before the tool ran, and the server recorded that itself.
        if successor is None or successor.envelope is None:
            return response
        if successor.envelope.status_code != TOOL_EXECUTION_FAILED:
            logger.warning(
                "gateway.tool_call.unexpected_error_status",
                run_id=run.run_id,
                status_code=successor.envelope.status_code,
            )
            return response
        # A tool execution failure is part of the chain: verify and advance.

    if successor is None or successor.envelope is None:
        return _reject(pending, "upstream response did not contain an AgentDNA successor workflow")

    if not _descends_from(successor, pending.request_workflow):
        return _reject(
            pending,
            "upstream AgentDNA successor workflow does not continue the workflow "
            "attached to this request",
        )

    try:
        verification_code = await asyncio.to_thread(context.dna.verify, successor)
    except Exception as exc:
        logger.error("gateway.tool_call.verify_error", run_id=run.run_id, error=str(exc))
        return _reject(pending, f"unable to verify upstream AgentDNA successor workflow: {exc}")

    if verification_code != RESULT_OK:
        await _record_verification_failure(pending, successor, verification_code)
        return _reject(
            pending,
            "upstream AgentDNA successor workflow failed verification",
            status_code=verification_code,
        )

    await context.complete_mcp_call(pending.handle, successor)
    pending.resolved = True
    return response


def _descends_from(successor: IntentWorkflow, request_workflow: IntentWorkflow) -> bool:
    """The successor's tip must be signed directly on top of our request.

    Without this a server could answer with any validly signed workflow - one
    from another run, or a replay - and it would pass verification.
    """

    request_envelope = request_workflow.envelope
    successor_envelope = successor.envelope
    if request_envelope is None or successor_envelope is None:
        return False
    if successor.id != request_workflow.id:
        return False
    return any(
        parent.hash == request_envelope.hash and parent.signature == request_envelope.signature
        for parent in successor_envelope.parent_envelope or []
    )


async def _record_verification_failure(
    pending: PendingCall,
    successor: IntentWorkflow,
    verification_code: int,
) -> None:
    """Put the failed verification on the provenance layer, as the adapters do."""

    dna = pending.run.context.dna
    try:
        failed = await asyncio.to_thread(
            dna.build,
            payload=(
                "CoCA: failed to verify signature for workflow received from the "
                f"{successor.get_latest_envelope_actor()}"
            ),
            previous_workflows=successor,
            verification_code=verification_code,
        )
        await asyncio.to_thread(dna.record, failed)
    except Exception as exc:
        # The agent is refused either way; a failed record must not change that.
        logger.error(
            "gateway.tool_call.record_failure_failed",
            run_id=pending.run.run_id,
            error=str(exc),
        )


def _reject(pending: PendingCall, message: str, status_code: int | None = None) -> dict:
    logger.warning(
        "gateway.tool_call.rejected",
        run_id=pending.run.run_id,
        reason=message,
        status_code=status_code,
    )
    data = {"agentdna_status_code": status_code} if status_code is not None else None
    return jsonrpc_error(pending.request_id, f"AgentDNA gateway: {message}", data)
