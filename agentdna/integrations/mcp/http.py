from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from mcp.types import CallToolResult

from agentdna.error import RESULT_OK

from .context import (
    MCPCallHandle,
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

    tool_name = getattr(
        request,
        "name",
        None,
    )

    if not isinstance(
        tool_name,
        str,
    ) or not tool_name:

        raise RuntimeError(
            "AgentDNA could not determine MCP tool name"
        )

    arguments = getattr(
        request,
        "arguments",
        None,
    )

    if arguments is None:

        arguments = getattr(
            request,
            "args",
            None,
        )

    # ------------------------------------------------------------
    # Begin observable MCP execution batch.
    # ------------------------------------------------------------

    call_handle = (
        await context.begin_mcp_call()
    )

    parent_frontier = list(
        call_handle.batch.parent_frontier
    )

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

    request_workflow = (
        context.dna.build(
            payload=request_payload,
            previous_workflows=(
                parent_frontier
            ),
            verification_code=RESULT_OK,
        )
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

    headers[
        AGENTDNA_HEADER_NAME
    ] = workflow_to_header(
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

    successor = (
        _extract_successor_workflow(
            result
        )
    )

    if successor is None:

        await context.cancel_mcp_call(
            call_handle
        )

        raise RuntimeError(
            "MCP tool response did not contain "
            "an AgentDNA successor workflow"
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

        raise ValueError(
            "AgentDNA MCP successor workflow "
            "verification failed"
        )

    # ------------------------------------------------------------
    # Record terminal MCP workflow.
    #
    # This may close the batch and advance the frontier.
    # ------------------------------------------------------------

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