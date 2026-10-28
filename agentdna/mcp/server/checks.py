from urllib.parse import urljoin

import requests
from fastmcp.server.middleware import MiddlewareContext

from agentdna.core import AgentDNA
from agentdna.error import (
    ADMIN_WHITELIST_CHECK_FAILED,
    ADMIN_WHITELIST_CHECK_SERVER_ERROR,
    COCA_VERIFICATION_FAILED_UNKNOWN,
    MIDDLEWARE_EXECUTION_FAILED,
    RESULT_OK,
)
from agentdna.types import (
    VERIFY_HEAVY,
    AgentNotWhitelistedError,
    CoCAVerificationError,
    IntentWorkflow,
)

from .types import CbacFn, CBACVerificationError
from .utils import (
    build_and_record_failed_workflow,
    get_tool_args,
    get_tool_description,
    get_tool_name,
)


def agent_whitelist_check(
    dna: AgentDNA,
    admin_server_url: str,
    incoming_workflow: IntentWorkflow,
):
    """
    Performs a whitelist check, by querying the Admin server, on every actor
    who signed an envelope in the incoming workflow.

    Args:
        dna (AgentDNA): The AgentDNA instance.
        admin_server_url (str): The URL of the Admin server.
        incoming_workflow (IntentWorkflow): IntentWorkflow to fetch actors from.

    Raises:
        RuntimeError: If any actor is not whitelisted or if the check fails.
    """
    actor_ids = incoming_workflow.list_actors()

    try:
        response = requests.post(
            urljoin(admin_server_url, "/agent-admin/v1/whitelist/check"),
            json={"dids": actor_ids},
            timeout=300,
        )
        response.raise_for_status()

        if not response.json()["status"]:
            raise Exception("failed to check whitelist from admin server")

        blacklist = response.json()["data"]["blacklist"]
        if len(blacklist) != 0:
            raise AgentNotWhitelistedError(f"Actors not whitelisted: {', '.join(blacklist)}")

    except AgentNotWhitelistedError as exc:
        build_and_record_failed_workflow(
            dna,
            payload=str(exc),
            incoming_workflows=incoming_workflow,
            verification_code=ADMIN_WHITELIST_CHECK_FAILED,
        )

        raise RuntimeError(str(exc))
    except Exception as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"Failed to check whitelist for actors in Admin server, error: {exc}",
            incoming_workflows=incoming_workflow,
            verification_code=ADMIN_WHITELIST_CHECK_SERVER_ERROR,
        )

        raise RuntimeError(f"Failed to check whitelist for actors in Admin server: {exc}") from exc


def coca_verification(
    dna: AgentDNA,
    agent_id: str,
    incoming_workflow: IntentWorkflow,
):
    """
    Performs CoCA verification for the incoming workflow from the requesting agent.

    Args:
        dna (AgentDNA): The AgentDNA instance.
        agent_id (str): The ID of the agent to verify.
        incoming_workflow (IntentWorkflow): The workflow to verify.

    Raises:
        RuntimeError: If CoCA verification fails.
    """
    try:
        verification_code = dna.verify(incoming_workflow, mode=VERIFY_HEAVY)

        if verification_code != RESULT_OK:
            raise CoCAVerificationError(f"CoCA verification failed for agent {agent_id}")
    except CoCAVerificationError as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"CoCA verification failed for agent {agent_id}",
            incoming_workflows=incoming_workflow,
            verification_code=verification_code,
        )

        raise RuntimeError(str(exc))
    except Exception as exc:
        build_and_record_failed_workflow(
            dna,
            payload=f"unable to perform CoCA verification for agent {agent_id}, error: {str(exc)}",
            incoming_workflows=incoming_workflow,
            verification_code=COCA_VERIFICATION_FAILED_UNKNOWN,
        )

        raise RuntimeError(
            f"Failed to perform CoCA verification for agent {agent_id}: {exc}"
        ) from exc


async def cbac_verification(
    server_dna: AgentDNA,
    agent_id: str,
    incoming_workflow: IntentWorkflow,
    cbac_fn: CbacFn,
    context: MiddlewareContext,
) -> tuple[str, int]:
    """
    Perform CBAC verification for the given agent and workflow using the provided CBAC function.

    Args:
        server_dna (AgentDNA): The AgentDNA instance of MCP Server.
        agent_id (str): The ID of the agent to verify.
        incoming_workflow (IntentWorkflow): The workflow to verify.
        cbac_fn (CbacFn): The CBAC function to call for verification.
        context (MiddlewareContext): The middleware context.

    Returns:
        tuple[str, int]: A tuple containing the CBAC message hash and status code.
    """
    cbac_message_hash: str = ""
    cbac_status: int = 0

    try:
        intent_id = incoming_workflow.id
        user_intent = incoming_workflow.get_root_envelope().payload
        tool_name = get_tool_name(context)
        tool_args = get_tool_args(context)
        tool_description = await get_tool_description(
            context,
            tool_name,
        )
        server_id = server_dna.get_actor_id()

        cbac_decision, cbac_status, cbac_message_hash = await cbac_fn(
            agent_id,
            server_id,
            tool_name,
            tool_args,
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
            server_dna,
            payload=cbac_message_hash,
            incoming_workflows=incoming_workflow,
            verification_code=cbac_status,
        )

        raise RuntimeError(f"Agent {agent_id} did not pass CBAC verification check: {exc}") from exc
    except Exception as exc:
        build_and_record_failed_workflow(
            server_dna,
            payload=f"unable to perform CBAC verification for agent {agent_id}: {str(exc)}",
            incoming_workflows=incoming_workflow,
            verification_code=MIDDLEWARE_EXECUTION_FAILED,
        )

        raise RuntimeError(
            f"unable to perform CBAC verification for agent {agent_id}: {exc}"
        ) from exc

    return cbac_message_hash, cbac_status
