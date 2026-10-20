"""IntentWorkflow propagation for one tools/call passing through the relay.
Transport-neutral: works on parsed JSON-RPC messages, so the HTTP layer only
has to find the messages and hand them over.
For each call:
    request   build a workflow on the run's frontier, put it in params._meta
    response  extract the successor, verify it, advance the frontier
Fail-closed. A successful tool result is only returned to the agent once its
successor has been verified. When it cannot be, the result is replaced by a
JSON-RPC error, so the agent never acts on output that is missing from the
provenance chain.

A server passes a successor down only for a success (1000) or a tool
execution failure (4001). Any other failure - a whitelist, CoCA or CBAC check,
say - is recorded on the provenance layer by the server, and a recorded
IntentWorkflow is never passed forward: that chain has ended. The server
reports it as an error result without a 4001 successor (FastMCP 3) or as a
JSON-RPC error (FastMCP 4); either one ends the run. The same holds
when the relay itself records a successor that failed verification. Either
way the run ends: the agent sees the error, every further tool call in the
run is refused, and closing the session raises instead of returning a frontier.
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
RELAY_ERROR = -32050

logger = get_logger("agentdna.relay")


class ToolCallRejected(Exception):
    """The relay could not attach an IntentWorkflow to a tools/call request"""


@dataclass
class PendingCall:
    """One tools/call that has been forwarded and not yet resolved"""

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
    error: dict[str, Any] = {"code": RELAY_ERROR, "message": message}

    if data:
        error["data"] = data

    return {"jsonrpc": "2.0", "id": request_id, "error": error}


async def begin_tool_call(run: Run, message: dict[str, Any]) -> PendingCall:
    """Build a workflow for this call and attach it to `message` in place.

    Unrelated `_meta` fields and namespaces are preserved. AgentDNA owns
    `_meta.agentdna` and overwrites whatever a client put there.
    """
    if run.ended is not None:
        raise ToolCallRejected(
            f"this run's chain has ended ({run.ended.reason}); no further tool calls are made"
        )

    params = message.get("params")
    if not isinstance(params, dict):
        raise ToolCallRejected("tools/call request has no param object")

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
    except Exception:
        await context.cancel_mcp_call(handle)
        raise

    meta = dict(existing_meta or {})
    agentdna_meta = dict(meta.get(AGENTDNA_META_KEY) or {})
    agentdna_meta.update(workflow_to_metadata(request_workflow)[AGENTDNA_META_KEY])
    meta[AGENTDNA_META_KEY] = agentdna_meta
    params["_meta"] = meta

    return PendingCall(
        run=run, handle=handle, request_id=message["id"], request_workflow=request_workflow
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
        # No successor was passed down: the server refused the call before or
        # in its checks. FastMCP 4 reports a middleware refusal this way, as an
        # "Internal server error", so it cannot be told apart from a protocol
        # error - fail closed and end the run either way.
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else None
        _end_run(run, str(message or "the MCP server refused the tool call"), None)
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
        if (
            successor is None
            or successor.envelope is None
            or successor.envelope.status_code != TOOL_EXECUTION_FAILED
        ):
            # The server refused the call - one of its checks failed - and
            # recorded it. That chain has ended, and so has the run.
            status_code = (
                successor.envelope.status_code if successor and successor.envelope else None
            )
            _end_run(
                run, _result_text(result) or "the MCP server refused the tool call", status_code
            )
            return response

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
        logger.error("relay.tool_call.verify_error", run_id=run.run_id, error=str(exc))
        return _reject(pending, f"unable to verify upstream AgentDNA successor workflow: {exc}")

    if verification_code != RESULT_OK:
        await _record_verification_failure(pending, successor, verification_code)
        _end_run(run, "an MCP server's answer failed AgentDNA verification", verification_code)
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


def _end_run(run: Run, reason: str, status_code: int | None) -> None:
    run.end(reason, status_code)
    logger.warning("agentdna.relay.run.ended", run_id=run.run_id, reason=reason, status_code=status_code)


def _result_text(result: dict[str, Any]) -> str:
    """The text of a tool result's content, as the server worded it."""

    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
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
            "relay.tool_call.record_failure_failed",
            run_id=pending.run.run_id,
            error=str(exc),
        )


def _reject(pending: PendingCall, message: str, status_code: int | None = None) -> dict:
    data = {"agentdna_status_code": status_code} if status_code is not None else None
    return jsonrpc_error(pending.request_id, f"AgentDNA relay: {message}", data)
