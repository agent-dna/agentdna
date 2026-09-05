from __future__ import annotations

import json
from typing import Any

from fastmcp.server.dependencies import (
    get_http_headers,
)
from fastmcp.server.middleware import (
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools.base import ToolResult

from agentdna import AgentDNA
from agentdna.core import IntentWorkflow
from agentdna.error import (
    TOOL_EXECUTION_FAILED,
    RESULT_OK,
)
from agentdna.types import load_workflow

from agentdna.integrations.mcp.context import agentdna_context
from agentdna.integrations.mcp.metadata import (
    AGENTDNA_HEADER_NAME,
    AGENTDNA_META_KEY,
    AGENTDNA_INTENT_WORKFLOW_META_KEY,
)
from .utils import  get_tool_name
from .types import CbacFn
from .checks import agent_whitelist_check, coca_verification, cbac_verification

class AgentDNAMCPMiddleware(Middleware):
    """
    FastMCP-specific AgentDNA server middleware.

    Responsibilities:

        1. Read incoming AgentDNA workflow.
        2. Verify it.
        3. Stop execution when verification fails.
        4. Execute the MCP tool.
        5. Build a successor event for success/failure.
        6. Attach the successor to MCP metadata.
    """
    def __init__(
        self,
        dna: AgentDNA,
        cbac_fn: CbacFn | None = None
    ) -> None:
        self.dna = dna
        self.cbac_fn = cbac_fn

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next,
    ) -> ToolResult:
        tool_name = get_tool_name(context)

        # Get AgentDNA from HTTP headers
        headers = get_http_headers() or {}
        workflow_header = headers.get(AGENTDNA_HEADER_NAME)

        if not workflow_header:
            raise ValueError(
                f"Missing required "
                f"{AGENTDNA_HEADER_NAME!r} header"
            )

        incoming_workflow = load_workflow(
            json.loads(
                workflow_header
            )
        )

        with agentdna_context(
            self.dna,
            incoming_workflow,
        ):  
            latest_envelope_actor = incoming_workflow.get_latest_envelope_actor()

            # CoCA verification
            coca_verification(
                self.dna,
                latest_envelope_actor,
                incoming_workflow,
            )

            # Whitelist check
            agent_whitelist_check(
                self.dna,
                self.dna.agentdna_admin_url,
                latest_envelope_actor,
                incoming_workflow,
            )

            # CBAC Verification
            if self.cbac_fn:
                await cbac_verification(
                    self.dna,
                    latest_envelope_actor,
                    incoming_workflow,
                    self.cbac_fn,
                    context
                )

            # Execute tool
            try:
                result = await call_next(
                    context
                )

                if not isinstance(result, ToolResult):
                    raise TypeError(
                        "FastMCP on_call_tool middleware expected "
                        f"ToolResult, got {type(result)!r}"
                    )

                successor_payload = (
                    json.dumps({
                            "type": (
                                "mcp_tool_result"
                            ),
                            "version": "1.0",
                            "tool": tool_name,
                            "status": (
                                "error"
                                if result.is_error
                                else "success"
                            ),
                        },
                        separators=(
                            ",",
                            ":",
                        ),
                        sort_keys=True,
                    )
                )

                successor = self.dna.build(
                    payload=successor_payload,
                    previous_workflows=(
                        incoming_workflow
                    ),
                    verification_code=RESULT_OK,
                )

                return (
                    _attach_agentdna_workflow(
                        result,
                        successor,
                    )
                )

            except Exception as exc:
                failure_payload = (
                    json.dumps(
                        {
                            "type": (
                                "mcp_tool_result"
                            ),
                            "version": "1.0",
                            "tool": tool_name,
                            "status": "error",
                            "error_type": (
                                type(exc).__name__
                            ),
                            "error": str(exc),
                        },
                        separators=(
                            ",",
                            ":",
                        ),
                        sort_keys=True,
                    )
                )

                failure_workflow = (
                    self.dna.build(
                        payload=(
                            failure_payload
                        ),
                        previous_workflows=(
                            incoming_workflow
                        ),
                        verification_code=(
                            TOOL_EXECUTION_FAILED
                        ),
                    )
                )

                failure_result = (
                    ToolResult(
                        content=(
                            "MCP tool execution failed: "
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        ),
                        is_error=True,
                    )
                )

                return (
                    _attach_agentdna_workflow(
                        failure_result,
                        failure_workflow,
                    )
                )


def _attach_agentdna_workflow(
    result: ToolResult,
    workflow: IntentWorkflow,
) -> ToolResult:

    existing_meta: dict[str, Any] = dict(
        result.meta
        or {}
    )

    existing_agentdna_meta: dict[str, Any] = dict(
        existing_meta.get(
            AGENTDNA_META_KEY
        )
        or {}
    )

    existing_agentdna_meta[
        AGENTDNA_INTENT_WORKFLOW_META_KEY
    ] = workflow.serialize()

    existing_meta[
        AGENTDNA_META_KEY
    ] = existing_agentdna_meta

    return result.model_copy(
        update={
            "meta": existing_meta,
        }
    )