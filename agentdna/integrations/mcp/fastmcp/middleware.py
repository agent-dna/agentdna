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
    ADMIN_WHITELIST_CHECK_FAILED,
    ADMIN_WHITELIST_CHECK_SERVER_ERROR,
    COCA_VERIFICATION_FAILED_UNKNOWN
)
from agentdna.types import (
    load_workflow, AgentNotWhitelistedError,
    CoCAVerificationError
)

from agentdna.integrations.mcp.context import agentdna_context
from agentdna.integrations.mcp.metadata import (
    AGENTDNA_HEADER_NAME,
    AGENTDNA_META_KEY,
    AGENTDNA_INTENT_WORKFLOW_META_KEY,
)
from agentdna.admin import request_agent_whitelist_check

class AgentDNAMCPMiddleware(
    Middleware
):
    """
    FastMCP-specific AgentDNA server middleware.

    Responsibilities:

        1. Read incoming AgentDNA workflow.
        2. Verify it.
        3. Stop execution when verification fails.
        4. Execute the MCP tool.
        5. Build a successor event for success/failure.
        6. Attach the successor to MCP metadata.

    It knows only about:

        - FastMCP
        - MCP
        - AgentDNA

    It does not know anything about Agent frameworks.
    """

    def __init__(
        self,
        dna: AgentDNA,
    ) -> None:

        self.dna = dna

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next,
    ) -> ToolResult:

        tool_name = context.message.name

        # ========================================================
        # 1. Read workflow header.
        # ========================================================

        headers = (
            get_http_headers()
            or {}
        )

        workflow_header = headers.get(
            AGENTDNA_HEADER_NAME
        )

        if not workflow_header:
            raise ValueError(
                f"Missing required "
                f"{AGENTDNA_HEADER_NAME!r} header"
            )

        # ========================================================
        # 2. Reconstruct workflow.
        # ========================================================

        incoming_workflow = (
            load_workflow(
                json.loads(
                    workflow_header
                )
            )
        )

        # ========================================================
        # 4. Establish request-local context.
        #
        # This is only for the downstream FastMCP execution path.
        # It does NOT represent an Agent interaction.
        # ========================================================

        with agentdna_context(
            self.dna,
            incoming_workflow,
        ):  
            latest_envelope_actor = incoming_workflow.get_latest_envelope_actor()

            # Whitelist check
            try:
                is_agent_whitelisted = request_agent_whitelist_check(
                    self.dna.agentdna_admin_url,
                    latest_envelope_actor
                )

                if not is_agent_whitelisted:
                    raise AgentNotWhitelistedError(
                        f"Agent {latest_envelope_actor} is not whitelisted in the Admin server."
                    )
            except AgentNotWhitelistedError as exc:
                workflow = self.dna.build(
                    payload=f"Agent {latest_envelope_actor} not whitelisted in Admin server",
                    previous_workflows=(
                        incoming_workflow
                    ),
                    verification_code=ADMIN_WHITELIST_CHECK_FAILED,
                )
                self.dna.record(
                    workflow
                )

                raise RuntimeError(
                    str(exc)
                )
            except Exception as exc:
                workflow = self.dna.build(
                    payload=f"unable to contact Admin server to check whitelist status for agent {latest_envelope_actor}",
                    previous_workflows=(
                        incoming_workflow
                    ),
                    verification_code=ADMIN_WHITELIST_CHECK_SERVER_ERROR,
                )
                self.dna.record(
                    workflow
                )

                raise RuntimeError(
                    f"Failed to check whitelist status for agent {latest_envelope_actor}: {exc}"
                ) from exc

            # CoCA verification
            try:
                verification_code = self.dna.verify(
                    incoming_workflow
                )

                if verification_code != RESULT_OK:
                    raise CoCAVerificationError(
                        f"AgentDNA IntentWorkflow verification failed for agent {latest_envelope_actor}"
                    )
            except CoCAVerificationError as exc:
                workflow = self.dna.build(
                    payload=f"CoCA verification failed for agent {latest_envelope_actor}",
                    previous_workflows=(
                        incoming_workflow
                    ),
                    verification_code=verification_code,
                )
                self.dna.record(
                    workflow
                )

                raise RuntimeError(
                    str(exc)
                )
            except Exception as exc:
                workflow = self.dna.build(
                    payload=f"unable to contact Admin server to check whitelist status for agent {latest_envelope_actor}",
                    previous_workflows=(
                        incoming_workflow
                    ),
                    verification_code=COCA_VERIFICATION_FAILED_UNKNOWN,
                )
                self.dna.record(
                    workflow
                )
                raise RuntimeError(
                    f"Failed to verify incoming workflow for agent {latest_envelope_actor}: {exc}"
                ) from exc

            # Execute tool
            try:
                result = await call_next(
                    context
                )

                if not isinstance(
                    result,
                    ToolResult,
                ):
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