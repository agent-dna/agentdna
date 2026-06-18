import logging
import os
import uvicorn

from agent import create_agent
from agent_executor import KarleyAgentExecutor
from dotenv import load_dotenv, find_dotenv
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)

load_dotenv(find_dotenv())
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class MissingAPIKeyError(Exception):
    """Exception for missing API key."""
    pass


def main():
    """Starts the agent server."""
    host = "localhost"
    port = 10002

    logger.info("🚀 Starting Karley agent server on %s:%s", host, port)

    try:
        use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI")
        google_api_key = os.getenv("GOOGLE_API_KEY")

        logger.debug("Env GOOGLE_GENAI_USE_VERTEXAI=%r, GOOGLE_API_KEY set=%r",
                     use_vertex, bool(google_api_key))

        if not use_vertex == "TRUE":
            if not google_api_key:
                raise MissingAPIKeyError(
                    "GOOGLE_API_KEY environment variable not set and GOOGLE_GENAI_USE_VERTEXAI is not TRUE."
                )

        capabilities = AgentCapabilities(streaming=True)
        skill = AgentSkill(
            id="check_schedule",
            name="Check Karley's Schedule",
            description="Checks Karley's availability for a pickleball game on a given date.",
            tags=["scheduling", "calendar"],
            examples=["Is Karley free to play pickleball tomorrow?"],
        )
        agent_card = AgentCard(
            name="Karley Agent",
            description="An agent that manages Karley's schedule for pickleball games.",
            url=f"http://{host}:{port}/",
            version="1.0.0",
            defaultInputModes=["text/plain"],
            defaultOutputModes=["text/plain"],
            capabilities=capabilities,
            skills=[skill],
        )
        logger.info("✅ AgentCard created: %s", agent_card.name)

        logger.debug("Calling create_agent()")
        adk_agent = create_agent()
        logger.info("✅ ADK agent created: %r", adk_agent)

        logger.debug("Creating Runner")
        runner = Runner(
            app_name="agents",
            agent=adk_agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService(),
        )
        logger.info("✅ Runner initialized")

        logger.debug("Initializing KarleyAgentExecutor")
        agent_executor = KarleyAgentExecutor(runner)
        logger.info("✅ KarleyAgentExecutor initialized")

        logger.debug("Creating DefaultRequestHandler and A2AStarletteApplication")
        request_handler = DefaultRequestHandler(
            agent_executor=agent_executor,
            task_store=InMemoryTaskStore(),
        )
        server = A2AStarletteApplication(
            agent_card=agent_card, http_handler=request_handler
        )
        logger.info("✅ A2AStarletteApplication created, starting uvicorn.run")

        uvicorn.run(server.build(), host=host, port=port)

    except MissingAPIKeyError as e:
        logger.error("❌ Error: %s", e)
        exit(1)
    except Exception as e:
        logger.exception("❌ An error occurred during server startup: %s", e)
        exit(1)


if __name__ == "__main__":
    main()