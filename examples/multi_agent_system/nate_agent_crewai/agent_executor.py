import json
import logging
from pathlib import Path
import sys

import os

sys.path.append(str(Path(__file__).resolve().parents[1]))

from a2a.utils.errors import ServerError
from agent import SchedulingAgent
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    InternalError,
    InvalidParamsError,
    Part,
    TextPart,
    UnsupportedOperationError,
)


from agentdna import AgentDNA

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SchedulingAgentExecutor(AgentExecutor):
    """AgentExecutor for the scheduling agent (Nate)."""

    def __init__(self):
        """Initializes the SchedulingAgentExecutor."""
        self.agent = SchedulingAgent()

        self.dna = AgentDNA(alias="nate", role="remote", api_key=os.environ.get("AGENTDNA_API_KEY"))
        logger.info("✅ Nate AgentDNA DID: %s", self.dna.trust.did)
        logger.info("✅ Nate Rubix base URL: %s", self.dna.trust.base_url)

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Executes the scheduling agent."""
        if not context.task_id or not context.context_id:
            raise ValueError("RequestContext must have task_id and context_id")
        if not context.message:
            raise ValueError("RequestContext must have a message")

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.submit()
        await updater.start_work()

        if self._validate_request(context):
            raise ServerError(error=InvalidParamsError())

        raw = context.get_user_input()
        print("📨 Incoming from Host – raw user input   :", raw)

        verify_info = await self.dna.handle(
            raw_text=raw,
            verify_mode="light",  
        )

        verified = verify_info["verified"]
        trust_issues     = verify_info["trust_issues"]
        if verified is False:
            logger.warning("Verification failed, check trust issues: %s", trust_issues)

        original_message = verify_info["original_message"]
        host_block       = verify_info["host_block"]
        host_ok          = verify_info["host_ok"]

        if host_ok is False:
            logger.warning("Host signature invalid: %s", trust_issues)
            # You *could* bail here instead:
            # raise ServerError(error=InternalError())

        logger.info(
            "🎯 Message for SchedulingAgent after trust layer: %r", original_message
        )

        try:
            result = self.agent.invoke(original_message)
            print(f"Final Result ===> {result}")
        except Exception as e:
            print(f"Error invoking agent: {e}")
            raise ServerError(error=InternalError()) from e

        built_resp = self.dna.build(
            original_message=original_message,
            response=result,
            host_block=host_block,
            extra={"host_trust_issues": trust_issues},  
        )

        # rubix_block = built_resp["rubix_block"]
        combined_json = built_resp["combined_json"]

        parts = [
            Part(root=TextPart(text=result)),        
            Part(root=TextPart(text=combined_json)), 
        ]

        await updater.add_artifact(parts)
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handles task cancellation."""
        raise ServerError(error=UnsupportedOperationError())

    def _validate_request(self, context: RequestContext) -> bool:
        """Validates the request context."""
        return False