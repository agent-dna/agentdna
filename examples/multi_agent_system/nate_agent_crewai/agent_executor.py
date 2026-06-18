import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv, find_dotenv

# Load shared MAS .env (walks up from this file until it finds one).
load_dotenv(find_dotenv())

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

        # Pure-remote agent — never writes to chain; enable_nft=False skips deploy.
        self.dna = AgentDNA(
            alias="nate",
            api_key=os.environ.get("AGENTDNA_API_KEY"),
            enable_nft=False,
        )
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

        ctx = await self.dna.handle(raw)
        if not ctx.verified:
            logger.warning("Host verification failed, trust_issues: %s", ctx.trust_issues)

        logger.info(
            "🎯 Message for SchedulingAgent after trust layer: %r", ctx.original_message
        )

        try:
            result = self.agent.invoke(ctx.original_message)
            print(f"Final Result ===> {result}")
        except Exception as e:
            print(f"Error invoking agent: {e}")
            raise ServerError(error=InternalError()) from e

        combined_json = self.dna.build(result, ctx=ctx)

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