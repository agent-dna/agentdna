from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from mcp.types import CallToolResult

from agentdna.error import RESULT_OK

from .context import (
    get_context,
)
from .metadata import (
    AGENTDNA_HEADER_NAME,
    workflow_from_header,
    workflow_to_header,
)


async def agentdna_http_interceptor(
    request: Any,
    handler: Callable[
        [Any],
        Awaitable[Any],
    ],
) -> Any:
    """
    AgentDNA MCP client interceptor.

    AgentDNA knows MCP here because MCP is an explicit protocol
    boundary.

    It does NOT know:

        - LangGraph
        - LangChain
        - ReAct
        - CrewAI
        - AutoGen
        - OpenAI Agents
        - model messages
        - framework tool-call IDs

    Observable batching rule:

        The first active MCP call creates a batch.

        MCP calls that start while that batch is active join it.

        Every call in that batch uses the same parent-frontier
        snapshot.

        When the final call completes, the terminal result workflows
        become the new causal frontier.
    """

    context = get_context()

    if context is None:
        return await handler(
            request
        )

    tool_name = getattr(request, "name", None)
    if not isinstance(tool_name,str) or not tool_name:
        raise RuntimeError(
            "AgentDNA could not determine MCP tool name"
        )

    arguments = getattr(request, "arguments", None)
    if arguments is None:
        arguments = getattr(
            request,
            "args",
            None,
        )

    # ------------------------------------------------------------
    # Begin observable MCP execution batch.
    # ------------------------------------------------------------
    call_handle = await context.begin_mcp_call()
    parent_frontier = list(call_handle.batch.parent_frontier)

    # ------------------------------------------------------------
    # Build MCP request event.
    # ------------------------------------------------------------

    request_payload = json.dumps(
        {
            "type": "mcp_tool_request",
            "version": "1.0",
            "tool": tool_name,
            "arguments": arguments,
        },
        separators=(
            ",",
            ":",
        ),
        sort_keys=True,
    )

    request_workflow = context.dna.build(
        payload=request_payload,
        previous_workflows=(
            parent_frontier
        ),
        verification_code=RESULT_OK,
    )

    # ------------------------------------------------------------
    # Inject workflow into HTTP request.
    # ------------------------------------------------------------

    headers = dict(
        getattr(
            request,
            "headers",
            {},
        )
        or {}
    )

    headers[AGENTDNA_HEADER_NAME] = workflow_to_header(
        request_workflow
    )

    request = request.override(
        headers=headers
    )

    # ------------------------------------------------------------
    # Execute remote MCP call.
    # ------------------------------------------------------------

    try:
        result = await handler(
            request
        )
    except Exception as exc:
        await context.cancel_mcp_call(
            call_handle
        )
        raise


    if not isinstance(
        result,
        CallToolResult,
    ):
        await context.cancel_mcp_call(
            call_handle
        )

        return result

    # ------------------------------------------------------------
    # Extract MCP server successor workflow.
    # ------------------------------------------------------------

    successor = _extract_successor_workflow(
        result
    )
    if successor is None:
        await context.cancel_mcp_call(
            call_handle
        )

        raise RuntimeError(
            _describe_missing_successor(
                result
            )
        )

    # ------------------------------------------------------------
    # SECURITY BOUNDARY
    # ------------------------------------------------------------

    verification_code = (
        context.dna.verify(
            successor
        )
    )

    if verification_code != RESULT_OK:
        await context.cancel_mcp_call(
            call_handle
        )

        failed_workflow = context.dna.build(
            payload=f"CoCA: failed to verify signature for workflow received from the {successor.get_latest_envelope_actor()}",
            previous_workflows=successor,
            verification_code=verification_code,
        )
        context.dna.record(failed_workflow)

        raise ValueError(
            f"CoCA verification failed for workflow recieved from the {successor.get_latest_envelope_actor()}"
        )

    await context.complete_mcp_call(
        call_handle,
        successor,
    )

    return result


def _extract_successor_workflow(
    result: CallToolResult,
):
    """
    Extract AgentDNA successor workflow from MCP metadata.
    """

    meta = result.meta

    if meta is None:
        return None

    if isinstance(
        meta,
        dict,
    ):

        agentdna = meta.get(
            "agentdna"
        )

    else:

        agentdna = getattr(
            meta,
            "agentdna",
            None,
        )

    if agentdna is None:

        return None

    if isinstance(
        agentdna,
        dict,
    ):

        serialized_workflow = (
            agentdna.get(
                "intent_workflow"
            )
        )

    else:
        serialized_workflow = None
        try:

            if hasattr(
                agentdna,
                "model_dump",
            ):

                dumped = agentdna.model_dump(
                    by_alias=True,
                    mode="json",
                    exclude_none=True,
                )

                if isinstance(
                    dumped,
                    dict,
                ):

                    serialized_workflow = (
                        dumped.get(
                            "intent_workflow"
                        )
                    )

        except Exception:

            serialized_workflow = None

    if not isinstance(
        serialized_workflow,
        str,
    ):

        return None

    return workflow_from_header(
        serialized_workflow
    )

def _describe_missing_successor(result: CallToolResult) -> str:
    """
    Produce a diagnostic explaining why the MCP successor could
    not be recovered.

    This is intentionally private. Framework/application
    developers should never need to import this helper.
    """

    meta = getattr(result, "meta", None)

    server_message = _extract_result_text(result)

    if meta is None:
        base = (
            "AgentDNA successor workflow was not propagated in "
            " the meta attribute"
        )

    elif isinstance(meta, dict):
        agentdna = meta.get(
            "agentdna"
        )

        if agentdna is None:
            base = (
                "AgentDNA successor workflow was not propagated: "
                "the MCP response metadata did not contain the "
                "'agentdna' field."
            )

        elif isinstance(agentdna, dict):
            if "intent_workflow" not in agentdna:
                base = (
                    "AgentDNA successor workflow was not propagated: "
                    "the MCP response contained an 'agentdna' object "
                    "but no 'intent_workflow' field."
                )

            elif not isinstance(
                agentdna.get(
                    "intent_workflow"
                ),
                str,
            ):
                base = (
                    "AgentDNA successor workflow was not propagated: "
                    "'agentdna.intent_workflow' was present but was "
                    "not a serialized workflow string."
                )

            else:
                base = (
                    "AgentDNA successor workflow was not propagated: "
                    "the returned workflow could not be reconstructed."
                )

        else:
            base = (
                "AgentDNA successor workflow was not propagated: "
                "the 'agentdna' response metadata had an unexpected "
                f"type: {type(agentdna).__name__}."
            )

    else:
        base = (
            "AgentDNA successor workflow was not propagated: "
            "the MCP response metadata had an unexpected "
            f"type: {type(meta).__name__}."
        )

    if server_message:
        return (
            f"{base} "
            f"MCP server message: {server_message}"
        )

    return (
        f"{base} "
        "No explanatory MCP server message was returned. "
        "Possible causes include an AgentDNA security/governance "
        "check rejecting the request, the server terminating "
        "propagation intentionally, or an unexpected server "
        "response format."
    )


def _extract_result_text(result: CallToolResult) -> str | None:
    """
    Extract human-readable text from an MCP CallToolResult.

    Private implementation detail.
    """

    content = getattr(result, "content", None)

    if not content:
        return None

    messages: list[str] = []

    for item in content:
        text = getattr(item, "text", None)

        if isinstance(text, str) and text:
            messages.append(text)

    if not messages:
        return None

    return " | ".join(messages)

