import json
import logging
from pathlib import Path
import sys
import os

from dotenv import load_dotenv
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
load_dotenv()

dna = AgentDNA(alias="kaitlynn", role="remote", api_key=os.environ.get("AGENTDNA_API_KEY"))
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

        verify_info = await dna.handle(
            raw_text=raw,
            verify_mode="light",   
        )

        verified      = verify_info["verified"]
        trust_issues  = verify_info["trust_issues"]
        original_msg  = verify_info["original_message"]
        host_block    = verify_info["host_block"]
        host_ok       = verify_info["host_ok"]

        if verified is False:
            logger.warning("Verification failed, trust issues: %s", trust_issues)
        if host_ok is False:
            logger.warning("Host signature invalid or missing: %s", trust_issues)

        print("Host verified:", host_ok)
        logger.info("🧾 Kaitlyn using original_message: %r", original_msg)

        try:
            async for item in self.agent.stream(original_msg, context.context_id):
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
                    agent_response = item["content"]

                    built_resp = dna.build(
                        original_message=original_msg,
                        response=agent_response,
                        host_block=host_block,
                        extra={"host_trust_issues": trust_issues},
                    )

                    combined_json = built_resp["combined_json"]

                    parts.append(
                        Part(root=TextPart(text=combined_json))
                    )

                    await updater.add_artifact(parts, name="scheduling_result")
                    await updater.complete()
                    break

        except Exception as e:
            logger.error(f"Error during execution: {e}")
            # Optional: mark task failed
            # await updater.update_status(TaskState.failed, message=TextPart(text=str(e)), final=True)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())