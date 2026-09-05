import contextlib

from fastmcp.server.middleware import (
    MiddlewareContext,
)

from agentdna.core import AgentDNA
from agentdna.types import IntentWorkflow

def build_and_record_failed_workflow(
        dna: AgentDNA, 
        payload: str, 
        incoming_workflows: IntentWorkflow | list[IntentWorkflow], 
        verification_code: int
):
    workflow = dna.build(
        payload=payload,
        previous_workflows=incoming_workflows,
        verification_code=verification_code,
    )
    dna.record(
        workflow
    )
    return workflow


def get_tool_name(context: MiddlewareContext) -> str:
    return context.message.name

def get_tool_args(context: MiddlewareContext) -> dict:
    return context.message.arguments

async def get_tool_description(
    context: MiddlewareContext,
    tool_name: str,
) -> str:
    fastmcp_context = context.fastmcp_context
    if fastmcp_context is None:
        raise RuntimeError(
            f"FastMCP context is not available"
        )

    fastmcp_server = fastmcp_context.fastmcp

    tool_description = ""
    with contextlib.suppress(Exception):
        tool_obj = await fastmcp_server.get_tool(tool_name)
        if tool_obj is None:

            raise RuntimeError(
                f"unexpected error: Tool {tool_name} not found"
            )

        tool_description = (tool_obj.description or "").partition("\n")[0] or ""

    return tool_description