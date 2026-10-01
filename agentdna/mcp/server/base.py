from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from mcp.server import ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.types import CallToolResult, TextContent

from agentdna import AgentDNA
from agentdna.auth import httpobserver
from agentdna.error import MIDDLEWARE_EXECUTION_FAILED, RESULT_OK, TOOL_EXECUTION_FAILED
from agentdna.mcp.context import agentdna_context
from agentdna.mcp.metadata import (
    workflow_from_metadata,
    workflow_to_metadata,
)
from agentdna.types import IntentWorkflow

from .checks import agent_whitelist_check, coca_verification
from .observer import record_request
from .types import CbacFn, CBACVerificationError
from .utils import build_and_record_failed_workflow


class AgentDNAMCPMiddleware:
    """
    AgentDNA middleware for the official MCP Python SDK v2.

    AgentDNA request propagation uses MCP `_meta`.

    Responsibilities:

        1. Extract the incoming AgentDNA workflow from MCP request `_meta`.
        2. Verify the workflow cryptographically.
        3. Run CoCA, whitelist, and optional CBAC checks.
        4. Execute the MCP request.
        5. Build a successor workflow for successful requests.
        6. Attach the successor to the MCP response metadata.
        7. Record tool execution failures as AgentDNA provenance failures.

    Currently instruments:

        - tools/call

    Unlike FastMCP middleware, this class operates directly on the official
    MCP SDK v2 ServerRequestContext.
    """

    def __init__(
        self,
        dna: AgentDNA,
        cbac_fn: CbacFn | None = None,
        supported_methods: set[str] | None = None,
    ) -> None:
        self.dna = dna
        self.cbac_fn = cbac_fn
        # Also record the calls this server makes to its own backend.
        # No-op unless evidence collection is switched on.
        httpobserver.install(dna)
        self.supported_methods = (
            supported_methods if supported_methods is not None else {"tools/call"}
        )

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if ctx.method not in self.supported_methods:
            return await call_next(ctx)

        if ctx.request_id is None:
            return await call_next(ctx)

        incoming_workflow = workflow_from_metadata(ctx.meta)
        if incoming_workflow is None:
            raise ValueError("No incoming workflow found in MCP request metadata")

        record_request(self.dna, incoming_workflow, ctx)

        # Everything in here is AgentDNA's own work - checks, CBAC, signing -
        # except the tool itself, so only the tool's outbound calls are hops.
        with (
            agentdna_context(
                self.dna,
                incoming_workflow,
            ),
            httpobserver.not_observed(),
        ):
            latest_envelope_actor = incoming_workflow.get_latest_envelope_actor()

            coca_verification(
                self.dna,
                latest_envelope_actor,
                incoming_workflow,
            )

            agent_whitelist_check(
                self.dna,
                self.dna.agentdna_admin_url,
                latest_envelope_actor,
                incoming_workflow,
            )

            if self.cbac_fn:
                await cbac_verification_mcp2(
                    dna=self.dna,
                    agent_id=latest_envelope_actor,
                    incoming_workflow=incoming_workflow,
                    cbac_fn=self.cbac_fn,
                    context=ctx,
                )

            try:
                with httpobserver.observed():
                    result = await call_next(ctx)

                successor = self._build_successor(
                    ctx=ctx,
                    incoming_workflow=incoming_workflow,
                    result=result,
                )

                return self._attach_successor(
                    result,
                    successor,
                )

            except Exception as exc:
                failure_workflow = self._build_execution_failure(
                    ctx=ctx,
                    incoming_workflow=incoming_workflow,
                    exc=exc,
                )

                self.dna.record(failure_workflow)

                return self._build_error_result(
                    result=exc,
                )

    def _build_successor(
        self,
        ctx: ServerRequestContext[Any, Any],
        incoming_workflow: IntentWorkflow,
        result: HandlerResult,
    ) -> IntentWorkflow:
        """
        Build the AgentDNA successor for a successful MCP request.
        """

        payload = json.dumps(
            {
                "type": "mcp_result",
                "version": "1.0",
                "method": ctx.method,
                "status": _result_status(result),
            },
            separators=(
                ",",
                ":",
            ),
            sort_keys=True,
        )

        return self.dna.build(
            payload=payload,
            previous_workflows=incoming_workflow,
            verification_code=RESULT_OK,
        )

    # ------------------------------------------------------------------
    # Tool/application failure provenance
    # ------------------------------------------------------------------

    def _build_execution_failure(
        self,
        ctx: ServerRequestContext[Any, Any],
        incoming_workflow: IntentWorkflow,
        exc: Exception,
    ) -> IntentWorkflow:
        """
        Build provenance for an MCP handler/application failure.
        """

        payload = json.dumps(
            {
                "type": "mcp_result",
                "version": "1.0",
                "method": ctx.method,
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            separators=(
                ",",
                ":",
            ),
            sort_keys=True,
        )

        return self.dna.build(
            payload=payload,
            previous_workflows=incoming_workflow,
            verification_code=TOOL_EXECUTION_FAILED,
        )

    @staticmethod
    def _build_error_result(
        result: Exception,
    ) -> CallToolResult:
        """
        Convert an MCP handler exception into an MCP tool error result.

        This keeps the error visible to MCP clients while preserving the
        AgentDNA failure record on the server.
        """

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(f"MCP tool execution failed: {type(result).__name__}: {result}"),
                )
            ],
            isError=True,
        )

    @staticmethod
    def _attach_successor(
        result: HandlerResult,
        successor: IntentWorkflow,
    ) -> HandlerResult:
        """
        Attach an AgentDNA successor to an MCP CallToolResult.

        Existing metadata is preserved.
        """

        if not isinstance(
            result,
            CallToolResult,
        ):
            return result

        existing_meta: dict[str, Any] = dict(result.meta or {})

        existing_agentdna_meta: dict[str, Any] = dict(existing_meta.get("agentdna") or {})

        agentdna_meta = workflow_to_metadata(successor).get("agentdna")

        if isinstance(
            agentdna_meta,
            dict,
        ):
            existing_agentdna_meta.update(agentdna_meta)

        existing_meta["agentdna"] = existing_agentdna_meta

        return result.model_copy(
            update={
                "meta": existing_meta,
            }
        )


def _result_status(
    result: HandlerResult,
) -> str:
    """
    Return a high-level status for an MCP result.
    """

    if isinstance(
        result,
        CallToolResult,
    ):
        return "error" if result.isError else "success"

    return "success"


async def cbac_verification_mcp2(
    dna: AgentDNA,
    agent_id: str,
    incoming_workflow: IntentWorkflow,
    cbac_fn: CbacFn,
    context: ServerRequestContext[Any, Any],
) -> tuple[str, int]:
    cbac_message_hash: str = ""
    cbac_status: int = 0

    try:
        if not isinstance(context.params, Mapping):
            raise ValueError("Missing MCP request parameters")

        tool_name = context.params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("Missing MCP tool name")

        tool_args = context.params.get("arguments", {})
        if tool_args is None:
            tool_args = {}

        if not isinstance(tool_args, Mapping):
            raise ValueError("MCP tool arguments must be an object")

        intent_id = incoming_workflow.id
        user_intent = incoming_workflow.get_root_envelope().payload
        server_id = dna.get_actor_id()

        # MCP tools/call does not contain the tool description.
        # The server middleware therefore supplies an empty description
        # unless a separate tool-metadata lookup is added later.
        tool_description = ""

        cbac_decision, cbac_status, cbac_message_hash = await cbac_fn(
            agent_id,
            server_id,
            tool_name,
            dict(tool_args),
            user_intent,
            tool_description,
            intent_id,
        )

        if cbac_decision.lower() != "allow":
            raise CBACVerificationError(
                f"CBAC verification failed for agent {agent_id} with status {cbac_status}"
            )

    except CBACVerificationError as exc:
        build_and_record_failed_workflow(
            dna,
            payload=cbac_message_hash,
            incoming_workflows=incoming_workflow,
            verification_code=cbac_status,
        )

        raise RuntimeError(f"Agent {agent_id} did not pass CBAC verification check: {exc}") from exc

    except Exception as exc:
        build_and_record_failed_workflow(
            dna,
            payload=(f"unable to perform CBAC verification for agent {agent_id}: {exc}"),
            incoming_workflows=incoming_workflow,
            verification_code=MIDDLEWARE_EXECUTION_FAILED,
        )

        raise RuntimeError(
            f"unable to perform CBAC verification for agent {agent_id}: {exc}"
        ) from exc

    return cbac_message_hash, cbac_status
