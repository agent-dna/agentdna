

from agentdna.core import AgentDNA
from agentdna.types import IntentWorkflow

from agentdna.error import (
    MIDDLEWARE_EXECUTION_FAILED,
    TOOL_EXECUTION_FAILED,
    RESULT_OK,
    ADMIN_WHITELIST_CHECK_FAILED,
    ADMIN_WHITELIST_CHECK_SERVER_ERROR,
    COCA_VERIFICATION_FAILED_UNKNOWN
)
from agentdna.types import (
    AgentNotWhitelistedError,
    CoCAVerificationError
)
from agentdna.admin import request_agent_whitelist_check

from .utils import build_and_record_failed_workflow, get_tool_args, get_tool_description, get_tool_name
from .types import CBACVerificationError
from fastmcp.server.middleware import MiddlewareContext
from .types import CbacFn

def agent_whitelist_check(
        dna: AgentDNA,
        admin_server_url: str,
        agent_id: str,
        incoming_workflow: IntentWorkflow | list[IntentWorkflow],
):
    try:
        is_whitelisted = request_agent_whitelist_check(
            admin_server_url,
            agent_id
        )

        if not is_whitelisted:
            raise AgentNotWhitelistedError()

    except AgentNotWhitelistedError as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"Agent {agent_id} not whitelisted in Admin server",
            incoming_workflows=incoming_workflow,
            verification_code=ADMIN_WHITELIST_CHECK_FAILED,
        )

        raise RuntimeError(
            str(exc)
        )
    except Exception:
        build_and_record_failed_workflow(
            dna,
            payload=f"Failed to check whitelist for agent {agent_id} in Admin server",
            incoming_workflows=incoming_workflow,
            verification_code=ADMIN_WHITELIST_CHECK_SERVER_ERROR,
        )

        raise RuntimeError(
            "Failed to check whitelist for agent "
            f"{agent_id} in Admin server"
        )


def coca_verification(
    dna: AgentDNA,
    agent_id: str,
    incoming_workflow: IntentWorkflow,
):
    try:
        verification_code = dna.verify(
            incoming_workflow
        )

        if verification_code != RESULT_OK:
            raise CoCAVerificationError(
                f"AgentDNA IntentWorkflow verification failed for agent {agent_id}"
            )
    except CoCAVerificationError as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"CoCA verification failed for agent {agent_id}",
            incoming_workflows=incoming_workflow,
            verification_code=verification_code,
        )

        raise RuntimeError(
            str(exc)
        )
    except Exception as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"unable to verify incoming workflow for agent {agent_id}, error: {str(exc)}",
            incoming_workflows=incoming_workflow,
            verification_code=COCA_VERIFICATION_FAILED_UNKNOWN,
        )

        raise RuntimeError(
            f"Failed to verify incoming workflow for agent {agent_id}: {exc}"
        ) from exc


async def cbac_verification(
    dna: AgentDNA,
    agent_id: str,
    incoming_workflow: IntentWorkflow,
    cbac_fn: CbacFn,
    context: MiddlewareContext,
):
    try:
        intent_id =  incoming_workflow.id
        user_intent = incoming_workflow.get_root_envelope().payload
        callee_type = "tool"
        tool_name = get_tool_name(context)
        tool_args = get_tool_args(context)
        tool_description = await get_tool_description(
            context,
            tool_name,
        )

        cbac_status, cbac_message_hash = await cbac_fn(
            agent_id,
            tool_name,
            tool_args,
            user_intent,
            tool_description,
            callee_type,
            intent_id,
        )
        if cbac_status != RESULT_OK:
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

        raise RuntimeError(
            str(exc)
        )
    except Exception as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"unable to perform CBAC verification for agent {agent_id}, error: {str(exc)}",
            incoming_workflows=incoming_workflow,
            verification_code=MIDDLEWARE_EXECUTION_FAILED,
        )

        raise RuntimeError(
            f"Failed to perform CBAC verification for agent {agent_id}: {exc}"
        ) from exc
    
