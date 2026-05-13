import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
import os
import sys

from dotenv import load_dotenv
from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.utils.errors import ServerError
from google.adk import Runner
from google.adk.events import Event
from google.genai import types
from a2a.types import (
    Part,
    TextPart,
    TaskState,
    UnsupportedOperationError,
)

import os

from agentdna import AgentDNA

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class KarleyAgentExecutor(AgentExecutor):
    """An AgentExecutor that runs Karley's ADK-based Agent."""

    def __init__(self, runner: Runner):
        logger.debug("KarleyAgentExecutor.__init__ START")
        self.runner = runner

        # Pure-remote agent — never writes to chain; enable_nft=False skips deploy.
        self.dna = AgentDNA(
            alias="karley",
            api_key=os.environ.get("AGENTDNA_API_KEY"),
            enable_nft=False,
        )

        logger.info("✅ Karley AgentDNA DID: %s", self.dna.trust.did)
        logger.info("✅ Karley Rubix base URL: %s", self.dna.trust.base_url)
        logger.debug("KarleyAgentExecutor.__init__ END (runner=%s)", runner)

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        logger.debug("🚀 KarleyAgentExecutor.execute CALLED")
        logger.debug(
            "execute() START: task_id=%s context_id=%s message=%s",
            context.task_id,
            context.context_id,
            context.message,
        )

        if not context.task_id or not context.context_id:
            raise ValueError("RequestContext must have task_id and context_id")
        if not context.message:
            raise ValueError("RequestContext must have a message")

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if not context.current_task:
            await updater.submit()
        await updater.start_work()

        raw = ""
        try:
            first_part = context.message.parts[0]
            if isinstance(first_part.root, TextPart):
                raw = first_part.root.text
        except Exception as e:
            logger.warning("Failed to extract raw text from message: %s", e)

        logger.debug("📨 Incoming from Host – raw user input: %r", raw)

        ctx = await self.dna.verify_request(raw)
        if not ctx.verified:
            logger.warning("Host verification failed, trust_issues: %s", ctx.trust_issues)
        logger.debug("Derived original_message for ADK: %r", ctx.original_message)

        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=ctx.original_message)],
        )

        session_id = context.context_id
        user_id = (
            context.message.metadata.get("user_id")
            if context.message and context.message.metadata
            else "karley_user"
        )

        logger.debug(
            "execute() starting ADK runner with session_id=%s user_id=%s",
            session_id,
            user_id,
        )

        agent_reply_text = ""

        try:
            session = await self.runner.session_service.get_session(
                app_name=self.runner.app_name,
                user_id=user_id,
                session_id=session_id,
            ) or await self.runner.session_service.create_session(
                app_name=self.runner.app_name,
                user_id=user_id,
                session_id=session_id,
            )

            final_event: Event | None = None

            async for event in self.runner.run_async(
                session_id=session.id,
                user_id=user_id,
                new_message=user_content,
            ):
                logger.debug("execute() got ADK event: %s", event)

                if event.is_final_response():
                    final_event = event
                    break

            if final_event and final_event.content and final_event.content.parts:
                texts = []
                for p in final_event.content.parts:
                    t = getattr(p, "text", None)
                    if t:
                        texts.append(t)
                agent_reply_text = "".join(texts)

        except Exception as e:
            logger.exception("Error while running ADK agent: %s", e)
            await updater.update_status(
                TaskState.failed,
                message=TextPart(text=f"An error occurred in KarleyAgentExecutor: {e}"),
                final=True,
            )
            return

        logger.debug("execute() ADK final reply text: %r", agent_reply_text)

        combined_json = self.dna.sign_response(agent_reply_text, ctx=ctx)

        parts = [
            Part(root=TextPart(text=agent_reply_text or "")),
            Part(root=TextPart(text=combined_json)),
        ]

        await updater.add_artifact(parts)
        await updater.complete()
        logger.debug("execute() END")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        logger.debug(
            "cancel() called for task_id=%s context_id=%s",
            context.task_id,
            context.context_id,
        )
        raise ServerError(error=UnsupportedOperationError())