import json
import logging
from pathlib import Path
import sys
import os

from dotenv import load_dotenv, find_dotenv
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    Part,
    TextPart,
    TaskState,
    UnsupportedOperationError,
)
from a2a.utils.errors import ServerError

import os

from app.agent import KaitlynAgent

from agentdna import AgentDNA

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv(find_dotenv())  # walks up to find the shared MAS .env

# Pure-remote agent — never writes to chain; enable_nft=False skips deploy.
dna = AgentDNA(
    alias="kaitlynn",
    api_key=os.environ.get("AGENTDNA_API_KEY"),
    enable_nft=False,
)
print("✅ Kaitlyn Using DID:", dna.trust.did)
print("✅ Kaitlyn Using base URL:", dna.trust.base_url)


class KaitlynAgentExecutor(AgentExecutor):
    """Kaitlyn's Scheduling AgentExecutor."""

    def __init__(self):
        self.agent = KaitlynAgent()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("RequestContext must have task_id and context_id")
        if not context.message:
            raise ValueError("RequestContext must have a message")

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.submit()
        await updater.start_work()

        raw = context.get_user_input()
        print("📨 Incoming from Host – raw user input:", raw)

        ctx = await dna.handle(raw)
        if not ctx.verified:
            logger.warning("Host verification failed, trust_issues: %s", ctx.trust_issues)

        print("Host verified:", ctx.verified)
        logger.info("🧾 Kaitlyn using original_message: %r", ctx.original_message)

        try:
            async for item in self.agent.stream(ctx.original_message, context.context_id):
                is_task_complete   = item["is_task_complete"]
                require_user_input = item.get("require_user_input", False)
                parts = [Part(root=TextPart(text=item["content"]))]

                if not is_task_complete and not require_user_input:
                    await updater.update_status(
                        TaskState.working,
                        message=updater.new_agent_message(parts),
                    )
                elif require_user_input:
                    await updater.update_status(
                        TaskState.input_required,
                        message=updater.new_agent_message(parts),
                    )
                    break
                else:
                    combined_json = dna.build(item["content"], ctx=ctx)
                    parts.append(Part(root=TextPart(text=combined_json)))

                    await updater.add_artifact(parts, name="scheduling_result")
                    await updater.complete()
                    break

        except Exception as e:
            logger.error(f"Error during execution: {e}")
            # Optional: mark task failed
            # await updater.update_status(TaskState.failed, message=TextPart(text=str(e)), final=True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())