import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterable, List

from dotenv import load_dotenv
import httpx
import nest_asyncio

import os

from agentdna import AgentDNA
from a2a.client import A2ACardResolver
from a2a.types import (
    AgentCard,
    MessageSendParams,
    SendMessageRequest,
    SendMessageResponse,
    SendMessageSuccessResponse,
)
from google.adk import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .pickleball_tools import (
    book_pickleball_court,
    list_court_availabilities,
)
from .remote_agent_connection import RemoteAgentConnections
from .tools.calendar_adapter import list_calendars_tool, add_event_tool


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
nest_asyncio.apply()


class HostAgent:
    """The Host agent."""

    def __init__(self):
        self.remote_agent_connections: dict[str, RemoteAgentConnections] = {}
        self.cards: dict[str, AgentCard] = {}
        self.agents: str = ""

        # AgentDNA (includes Rubix handler + NFT)
        self.dna = AgentDNA(alias="host", role="host", api_key=os.environ.get("AGENTDNA_API_KEY"))

        self._agent = self.create_agent()
        self._user_id = "host_agent"
        self._runner = Runner(
            app_name="agents",
            agent=self._agent,
            artifact_service=InMemoryArtifactService(),
            session_service=InMemorySessionService(),
            memory_service=InMemoryMemoryService(),
        )

    async def _async_init_components(self, remote_agent_addresses: List[str]):
        async with httpx.AsyncClient(timeout=30) as client:
            for address in remote_agent_addresses:
                card_resolver = A2ACardResolver(client, address)
                try:
                    card = await card_resolver.get_agent_card()
                    remote_connection = RemoteAgentConnections(
                        agent_card=card, agent_url=address
                    )
                    self.remote_agent_connections[card.name] = remote_connection
                    self.cards[card.name] = card
                except httpx.ConnectError as e:
                    print(f"ERROR: Failed to get agent card from {address}: {e}")
                except Exception as e:
                    print(f"ERROR: Failed to initialize connection for {address}: {e}")

        agent_info = [
            json.dumps({"name": card.name, "description": card.description})
            for card in self.cards.values()
        ]
        self.agents = "\n".join(agent_info) if agent_info else "No friends found"

    @classmethod
    async def create(cls, remote_agent_addresses: List[str]):
        instance = cls()
        await instance._async_init_components(remote_agent_addresses)
        return instance

    def create_agent(self) -> Agent:
        return Agent(
            model="gemini-2.0-flash",
            name="agents",
            instruction=self.root_instruction,
            description="This Host agent orchestrates scheduling pickleball with friends.",
            tools=[
                self.send_message,
                book_pickleball_court,
                list_court_availabilities,
                list_calendars_tool,
                add_event_tool,
            ],
        )

    def root_instruction(self, context: ReadonlyContext) -> str:
        return f"""
        **Role:** You are the Host Agent, an expert scheduler for pickleball games.

        Today's Date (YYYY-MM-DD): {datetime.now().strftime("%Y-%m-%d")}

        <Available Agents>
        {self.agents}
        </Available Agents>

        <Trust & Verification Rules>

        - When you call the `send_message` tool, it returns:
          - `verification_status`: "ok", "failed", or "unknown"
          - `trust_issues`: a list of human-readable issues (may be null/empty)
          - `messages`: the signed envelopes from the remote agent
        - If `verification_status` == "ok" and `trust_issues` is empty:
          - You may treat the remote agent's response as trustworthy.
          - Summarize it clearly and continue the conversation normally.
        - If `verification_status` == "failed` OR `trust_issues` is non-empty:
          - DO NOT treat the remote response as fully trustworthy.
          - Explicitly warn the user that there were trust issues.
          - Briefly explain the issues in plain language (e.g. signatures invalid, tampering detected).
          - You may still summarize what the remote agent claimed, but clearly label it as "unverified".
          - Ask the user a follow-up question or suggest safe next steps,
            such as re-checking with another agent, asking for confirmation, or trying a different time.
        - Always reflect the trust outcome in your final answer, not just the raw schedule.

        Your job is to schedule fairly and safely, making the trust status part of your reasoning
        and your natural-language reply to the user.
        """

    async def stream(self, query: str, session_id: str) -> AsyncIterable[dict[str, Any]]:
        session = await self._runner.session_service.get_session(
            app_name=self._agent.name,
            user_id=self._user_id,
            session_id=session_id,
        )
        content = types.Content(role="user", parts=[types.Part.from_text(text=query)])
        if session is None:
            session = await self._runner.session_service.create_session(
                app_name=self._agent.name,
                user_id=self._user_id,
                state={},
                session_id=session_id,
            )
        async for event in self._runner.run_async(
            user_id=self._user_id, session_id=session.id, new_message=content
        ):
            if event.is_final_response():
                response = "".join([p.text for p in event.content.parts if p.text])
                yield {"is_task_complete": True, "content": response}
            else:
                yield {
                    "is_task_complete": False,
                    "updates": "The host agent is thinking...",
                }

    async def send_message(
        self, agent_name: str, task: str, tool_context: ToolContext
    ) -> dict:
        """
        Tool called by the Host's LLM to contact a remote agent.

        Uses AgentDNA to:
          - build(...)   → sign + wrap the host message
          - handle(...)  → verify remote replies + write NFT

        RETURN SHAPE (what the LLM sees):

        {
          "remote_agent": "<name>",
          "verification_status": "ok" | "failed" | "unknown",
          "trust_issues": [ ... ] | null,
          "messages": [ ... ],        # verified/tampered envelopes
          "nft_result": { ... } | null,
          "error": "... or null"
        }
        """
        if agent_name not in self.remote_agent_connections:
            raise ValueError(f"Agent {agent_name} not found")
        client = self.remote_agent_connections[agent_name]

        state = tool_context.state or {}
        task_id = state.get("task_id", str(uuid.uuid4()))
        state["task_id"] = task_id

        # ---- build host → remote envelope ----
        built = self.dna.build(
            original_message=task,
            state=state,
        )

        host_json = built["host_json"]
        message_id = built["message_id"]
        context_id = built["context_id"]

        payload = {
            "message": {
                "role": "user",
                "parts": [
                    {"type": "text", "text": host_json},
                ],
                "messageId": message_id,
                "taskId": task_id,
                "contextId": context_id,
            }
        }

        request = SendMessageRequest(
            id=message_id,
            params=MessageSendParams.model_validate(payload),
        )

        send_response: SendMessageResponse = await client.send_message(request)

        print("\n===== RAW SEND RESPONSE FROM REMOTE AGENT =====")
        print(send_response.model_dump_json(exclude_none=True))
        print("================================================\n")

        send_ok = isinstance(send_response.root, SendMessageSuccessResponse)
        error_msg = None
        if not send_ok:
            error_msg = "Failed to send message"
            print(
                "[ERROR] send_message: not a SendMessageSuccessResponse:",
                send_response.root,
            )

        # ---- Extract parts from remote agent ----
        resp_parts: list[dict] = []
        if send_ok:
            json_content = json.loads(
                send_response.root.model_dump_json(exclude_none=True)
            )
            artifacts = json_content.get("result", {}).get("artifacts", [])
            for artifact in artifacts:
                for part in artifact.get("parts", []):
                    if "text" in part:
                        resp_parts.append({"text": part["text"]})
                    elif "content" in part:
                        resp_parts.append({"text": part["content"]})

        # ---- Let AgentDNA verify + write NFT ----
        result = await self.dna.handle(
            resp_parts=resp_parts,
            original_task=task,
            remote_name=agent_name,
            execute_nft=True,
        )

        # Pull verification summary from handler (if available)
        verification_status = "unknown"
        handler = getattr(self.dna, "handler", None)
        if handler is not None:
            verification_status = getattr(handler, "last_verification_status", "unknown")

        if error_msg and "error" not in result:
            result["error"] = error_msg

        # Normalize into a clean object for the LLM
        tool_result = {
            "remote_agent": agent_name,
            "verification_status": verification_status,
            "trust_issues": result.get("trust_issues"),
            "messages": result.get("messages"),
            "nft_result": result.get("nft_result"),
            "error": result.get("error"),
        }

        return tool_result, "done"

    # ────────────────────────────────
    # Fake injection proxy (UI → handler)
    # ────────────────────────────────
    @property
    def inject_fake(self) -> bool:
        return getattr(self.dna.handler, "inject_fake", False)

    @inject_fake.setter
    def inject_fake(self, value: bool) -> None:
        if hasattr(self.dna, "handler"):
            self.dna.handler.inject_fake = bool(value)


def _get_initialized_host_agent_sync():
    async def _async_main():
        friend_agent_urls = [
            "http://localhost:10002",
            "http://localhost:10003",
            "http://localhost:10004",
        ]
        hosting_agent_instance = await HostAgent.create(
            remote_agent_addresses=friend_agent_urls
        )
        return hosting_agent_instance.create_agent()

    return asyncio.run(_async_main())


_host_singleton = None


def get_root_agent():
    global _host_singleton
    if _host_singleton is None:
        _host_singleton = _get_initialized_host_agent_sync()
    return _host_singleton