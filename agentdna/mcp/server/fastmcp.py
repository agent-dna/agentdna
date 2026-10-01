from __future__ import annotations

import json
from typing import Any

from fastmcp.server.middleware import (
    Middleware,
    MiddlewareContext,
)

try:
    from fastmcp.tools import ToolResult
except ImportError:
    from fastmcp.tools.base import ToolResult

from agentdna import AgentDNA
from agentdna.auth import httpobserver
from agentdna.error import (
    RESULT_OK,
    TOOL_EXECUTION_FAILED,
)
from agentdna.mcp.context import agentdna_context
from agentdna.mcp.metadata import (
    workflow_from_metadata,
    workflow_to_metadata,
)
from agentdna.types import IntentWorkflow

from .checks import (
    agent_whitelist_check,
    cbac_verification,
    coca_verification,
)
from .observer import record_request
from .types import CbacFn
from .utils import get_tool_name


class AgentDNAMCPMiddleware(Middleware):
    """
    FastMCP-specific AgentDNA server middleware.

    AgentDNA request propagation uses MCP `_meta`.

    Responsibilities:

        1. Read incoming AgentDNA workflow from MCP request `_meta`.
        2. Verify the workflow.
        3. Run AgentDNA security/governance checks.
        4. Verify that the workflow describes the actual MCP call.
        5. Execute the MCP tool.
        6. Build a successor workflow.
        7. Attach the successor to MCP response metadata.
    """

    def __init__(
        self,
        dna: AgentDNA,
        cbac_fn: CbacFn | None = None,
    ) -> None:
        self.dna = dna
        self.cbac_fn = cbac_fn
        # Also record the calls this server makes to its own backend.
        # No-op unless evidence collection is switched on.
        httpobserver.install(dna)

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next,
    ) -> ToolResult:
        tool_name = get_tool_name(context)

        incoming_workflow = _extract_workflow(context)
        if incoming_workflow is None:
            raise ValueError("Missing AgentDNA workflow in MCP request metadata")

        record_request(self.dna, incoming_workflow, context)

        # Everything in here is AgentDNA's own work - checks, CBAC, signing -
        # except the tool itself, so only the tool's outbound calls are hops.
        with agentdna_context(self.dna, incoming_workflow), httpobserver.not_observed():
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

            # CBAC verification
            if self.cbac_fn:
                await cbac_verification(
                    self.dna,
                    latest_envelope_actor,
                    incoming_workflow,
                    self.cbac_fn,
                    context,
                )

            # Execute tool
            try:
                with httpobserver.observed():
                    result = await call_next(context)
                if not isinstance(result, ToolResult):
                    raise TypeError(
                        f"FastMCP on_call_tool middleware expected ToolResult, got {type(result)!r}"
                    )

                successor_payload = json.dumps(
                    {
                        "type": ("mcp_tool_result"),
                        "version": "1.0",
                        "tool": tool_name,
                        "status": ("error" if result.is_error else "success"),
                    },
                    separators=(
                        ",",
                        ":",
                    ),
                    sort_keys=True,
                )

                successor = self.dna.build(
                    payload=successor_payload,
                    previous_workflows=incoming_workflow,
                    verification_code=RESULT_OK,
                )

                return _attach_agentdna_workflow(
                    result,
                    successor,
                )

            except Exception as exc:
                failure_payload = json.dumps(
                    {
                        "type": ("mcp_tool_result"),
                        "version": "1.0",
                        "tool": tool_name,
                        "status": "error",
                        "error_type": (type(exc).__name__),
                        "error": str(exc),
                    },
                    separators=(
                        ",",
                        ":",
                    ),
                    sort_keys=True,
                )

                failure_workflow = self.dna.build(
                    payload=failure_payload,
                    previous_workflows=incoming_workflow,
                    verification_code=TOOL_EXECUTION_FAILED,
                )

                failure_result = ToolResult(
                    content=(f"MCP tool execution failed: {type(exc).__name__}: {exc}"),
                    is_error=True,
                )

                return _attach_agentdna_workflow(
                    failure_result,
                    failure_workflow,
                )


def _extract_workflow(
    context: MiddlewareContext,
) -> IntentWorkflow:
    """
    Diagnostic extraction of AgentDNA workflow from the FastMCP request.
    """

    fastmcp_context = getattr(context, "fastmcp_context", None)

    if fastmcp_context is not None:
        request_context = getattr(fastmcp_context, "request_context", None)

        if request_context is None:
            raise ValueError("Missing required FastMCP request context")

    # ------------------------------------------------------------
    # Try the raw MCP message first.
    # ------------------------------------------------------------

    message_params = getattr(context.message, "params", None)

    meta = None

    if message_params is not None:
        meta = getattr(message_params, "meta", None)

    if meta is None and fastmcp_context is not None:
        request_context = getattr(fastmcp_context, "request_context", None)

        if request_context is not None:
            meta = getattr(request_context, "meta", None)

    workflow = workflow_from_metadata(meta)

    if workflow is None:
        raise ValueError(
            "Missing required AgentDNA IntentWorkflow "
            "in MCP request `_meta` under "
            "'agentdna.intent_workflow'"
        )

    return workflow


def _attach_agentdna_workflow(
    result: ToolResult,
    workflow: IntentWorkflow,
) -> ToolResult:
    """
    Attach an AgentDNA successor workflow to MCP response metadata.

    Existing metadata is preserved.

    AgentDNA owns:

        meta["agentdna"]["intent_workflow"]
    """

    existing_meta: dict[str, Any] = dict(result.meta or {})

    existing_agentdna_meta: dict[str, Any] = dict(existing_meta.get("agentdna") or {})

    agentdna_meta = workflow_to_metadata(workflow).get("agentdna")

    if isinstance(agentdna_meta, dict):
        existing_agentdna_meta.update(agentdna_meta)

    existing_meta["agentdna"] = existing_agentdna_meta

    return result.model_copy(
        update={
            "meta": existing_meta,
        }
    )
