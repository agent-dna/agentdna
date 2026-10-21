"""The swarm's wiring and its chain of custody, driven by a scripted model: no network, no keys.

The scripted model plays every member. Each specialist calls its tool once and
then hands off to the next; the coordinator routes the work and writes the
report. The run goes through ADK's real runner, transfers and tool calls, with
stand-in AgentDNA identities and a stand-in Provenance Relay whose sessions sign
tool calls the way the real one does.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator

import pytest
import swarm
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types
from swarm import TOOLS, Custody, Handoff, SessionToolset, build_swarm, run_swarm

from agentdna.helpers import _hash_content
from agentdna.mcp.relay import ChainTerminated
from agentdna.types import Envelope, IntentWorkflow

# Each specialist's scripted turn: (tool to call, its arguments, who to hand off to).
SCRIPT = {
    "package_agent": ("package_info", {"name": "demo"}, "security_agent"),
    "security_agent": (
        "find_vulnerabilities",
        {"name": "demo", "version": "2.0.0"},
        "ecosystem_agent",
    ),
    "ecosystem_agent": ("dependencies", {"name": "demo", "version": "2.0.0"}, "coordinator"),
}


def _call(name: str, args: dict) -> LlmResponse:
    part = types.Part(function_call=types.FunctionCall(name=name, args=args))
    return LlmResponse(content=types.Content(role="model", parts=[part]))


class ScriptedLlm(BaseLlm):
    model: str = "scripted"

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["scripted"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        instruction = str(llm_request.config.system_instruction)
        agent = next(
            name for name in TOOLS if f"You are {name}." in instruction.replace("the ", "")
        )
        history = str(llm_request.contents)

        if agent == "coordinator":
            if "dependencies" in history:  # the last specialist has reported back
                text = types.Part(text="REPORT: demo 2.0.0 - adopt")
                yield LlmResponse(content=types.Content(role="model", parts=[text]))
            else:
                yield _call("transfer_to_agent", {"agent_name": "package_agent"})
            return

        tool, args, next_agent = SCRIPT[agent]
        own_results = [
            part.function_response
            for content in llm_request.contents
            for part in content.parts or []
            if part.function_response and part.function_response.name == tool
        ]
        if own_results:
            yield _call("transfer_to_agent", {"agent_name": next_agent})
        else:
            yield _call(tool, args)


# -- stand-ins for AgentDNA ------------------------------------------------------------


class FakeIdentity:
    """Signs by naming itself; enough to give every envelope a unique identity."""

    def __init__(self, name: str) -> None:
        self.name = name

    def get_actor_id(self) -> str:
        return f"did:{self.name}"

    def build(self, payload, previous_workflows=None, recipient_id="", verification_code=1000):
        previous = previous_workflows or []
        if isinstance(previous, IntentWorkflow):
            previous = [previous]
        envelope = Envelope(
            from_=self.get_actor_id(),
            payload=payload,
            epoch=time.time_ns(),
            status_code=verification_code,
            to=recipient_id,
        )
        for workflow in previous:
            envelope.add_envelope(workflow.envelope)
        envelope.hash = _hash_content(envelope)
        envelope.signature = f"{self.get_actor_id()}|{envelope.hash}"
        return IntentWorkflow(
            id=previous[0].id if previous else "intent",
            type="intent_workflow",
            version="2.0",
            envelope=envelope,
        )


USER, SERVER = FakeIdentity("user"), FakeIdentity("server")


class FakeSession:
    """A relay session: tool calls are signed by the agent on the frontier, and answered."""

    def __init__(self, relay: "FakeRelay", agent: FakeIdentity, received) -> None:
        self.relay = relay
        self.agent = agent
        self.received = received
        self.frontier = [received] if isinstance(received, IntentWorkflow) else list(received)
        self.closed = False
        self.forced = False
        self.ended: str | None = None  # set when a server refuses a call

    async def aopen(self):
        self.relay.current = self
        return self

    def url(self, server: str) -> str:
        return f"fake://{server}"

    async def aclose(self, force: bool = False):
        self.closed, self.forced = True, force
        if self.ended is not None:
            raise ChainTerminated(410, {"reason": self.ended})
        return list(self.frontier)

    def call(self, tool: str) -> None:
        request = self.agent.build(
            payload=json.dumps({"method": "tools/call", "params": {"name": tool}}),
            previous_workflows=self.frontier,
        )
        self.frontier = [SERVER.build(payload=f"{tool} result", previous_workflows=request)]


class FakeRelay:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.current: FakeSession | None = None

    def session(self, agent, workflows):
        session = FakeSession(self, agent, workflows)
        self.sessions.append(session)
        return session


@pytest.fixture
def relay(monkeypatch):
    relay = FakeRelay()
    members = {name: FakeIdentity(name) for name in TOOLS}
    monkeypatch.setattr(swarm, "PROVENANCE_RELAY", relay)
    monkeypatch.setattr(swarm, "identities", lambda: members)
    return relay


def stub_tools(relay: FakeRelay) -> dict:
    """The specialists' tools, each making its call through the current relay session."""

    def package_info(name: str) -> dict:
        relay.current.call("package_info")
        return {"name": name, "latest_version": "2.0.0"}

    def find_vulnerabilities(name: str, version: str) -> list:
        relay.current.call("find_vulnerabilities")
        return []

    def dependencies(name: str, version: str) -> dict:
        relay.current.call("dependencies")
        return {"direct": [], "indirect": [], "total": 0}

    return {
        "package_agent": [package_info],
        "security_agent": [find_vulnerabilities],
        "ecosystem_agent": [dependencies],
    }


def run(relay, model=None):
    incoming = USER.build(payload="Should we adopt demo?")
    result = asyncio.run(run_swarm(incoming, tools=stub_tools(relay), model=model or ScriptedLlm()))
    return incoming, result


def label(envelope: Envelope) -> str:
    try:
        payload = json.loads(envelope.payload)
    except ValueError:
        return envelope.payload
    if payload.get("type") == "handoff":
        return f"handoff {payload['from']} -> {payload['to']}"
    return f"call {payload['params']['name']}"


# -- the swarm ------------------------------------------------------------------------


def test_every_member_can_hand_off_to_every_other(relay):
    coordinator = build_swarm(
        stub_tools(relay), Custody(USER.build(payload="x"), {}), ScriptedLlm()
    )
    specialists = coordinator.sub_agents
    assert [a.name for a in specialists] == ["package_agent", "security_agent", "ecosystem_agent"]
    # Children reach their parent and, as peers, each other.
    assert all(
        not a.disallow_transfer_to_parent and not a.disallow_transfer_to_peers for a in specialists
    )
    assert coordinator.tools == []


def test_a_request_flows_through_the_swarm_and_back_to_the_coordinator(relay):
    _, result = run(relay)

    assert result.handoffs == [
        Handoff("coordinator", "package_agent"),
        Handoff("package_agent", "security_agent"),
        Handoff("security_agent", "ecosystem_agent"),
        Handoff("ecosystem_agent", "coordinator"),
    ]
    assert result.tool_calls == [
        ("package_agent", "package_info"),
        ("security_agent", "find_vulnerabilities"),
        ("ecosystem_agent", "dependencies"),
    ]
    assert (result.answered_by, result.result) == ("coordinator", "REPORT: demo 2.0.0 - adopt")


def test_a_run_without_an_answer_is_an_error_and_closes_its_session(relay):
    class Silent(ScriptedLlm):
        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="")]))

    with pytest.raises(RuntimeError, match="without a final answer"):
        run(relay, Silent())
    assert all(session.closed for session in relay.sessions)


# -- the chain of custody --------------------------------------------------------------


def test_each_turn_is_a_session_on_the_workflow_that_member_received(relay):
    incoming, _ = run(relay)

    turns = [(s.agent.name, s.received) for s in relay.sessions]
    assert [name for name, _ in turns] == [
        "coordinator",
        "package_agent",
        "security_agent",
        "ecosystem_agent",
        "coordinator",
    ]
    assert turns[0][1] is incoming  # the user's request, to the coordinator
    assert [label(received.envelope) for _, received in turns[1:]] == [
        "handoff coordinator -> package_agent",
        "handoff package_agent -> security_agent",
        "handoff security_agent -> ecosystem_agent",
        "handoff ecosystem_agent -> coordinator",
    ]
    assert all(s.closed and not s.forced for s in relay.sessions)


def test_the_chain_records_exactly_the_flow_that_happened(relay):
    incoming, result = run(relay)

    # Walk the chain from the answer back to the user's request: it is one
    # line, each envelope signed by the member that acted, on the one before.
    chain, envelope = [], result.adna_workflow.envelope
    while envelope is not None:
        chain.append((envelope.from_, label(envelope)))
        parents = envelope.parent_envelope or []
        assert len(parents) <= 1
        envelope = parents[0] if parents else None

    assert list(reversed(chain)) == [
        ("did:user", "Should we adopt demo?"),
        ("did:coordinator", "handoff coordinator -> package_agent"),
        ("did:package_agent", "call package_info"),
        ("did:server", "package_info result"),
        ("did:package_agent", "handoff package_agent -> security_agent"),
        ("did:security_agent", "call find_vulnerabilities"),
        ("did:server", "find_vulnerabilities result"),
        ("did:security_agent", "handoff security_agent -> ecosystem_agent"),
        ("did:ecosystem_agent", "call dependencies"),
        ("did:server", "dependencies result"),
        ("did:ecosystem_agent", "handoff ecosystem_agent -> coordinator"),
        ("did:coordinator", "REPORT: demo 2.0.0 - adopt"),
    ]
    assert result.adna_workflow.id == incoming.id


def test_hand_offs_are_addressed_to_the_receiver(relay):
    _, result = run(relay)

    handoffs = [e for e in result.adna_workflow.unwrap() if label(e).startswith("handoff ")]
    assert {(e.from_, e.to) for e in handoffs} == {
        ("did:coordinator", "did:package_agent"),
        ("did:package_agent", "did:security_agent"),
        ("did:security_agent", "did:ecosystem_agent"),
        ("did:ecosystem_agent", "did:coordinator"),
    }


def test_a_session_toolset_follows_the_current_session():
    toolset = SessionToolset(["package_info"])
    assert toolset._use_invocation_cache is False

    asyncio.run(toolset.use("http://relay/runs/a/mcp/packages"))
    first = toolset.current
    asyncio.run(toolset.use("http://relay/runs/b/mcp/packages"))

    assert toolset.current is not first
    assert toolset.current._connection_params.url == "http://relay/runs/b/mcp/packages"
    assert toolset.current.tool_filter == ["package_info"]


def test_a_refused_tool_call_stops_the_run(relay):
    """The server refused package_agent's call and recorded it: the chain has
    ended, so the run stops at the next hand-off, and nothing is signed on it."""

    tools = stub_tools(relay)

    def package_info(name: str) -> dict:
        relay.current.ended = "did not pass CBAC verification"
        return {"isError": True}

    tools["package_agent"] = [package_info]

    with pytest.raises(ChainTerminated) as ended:
        asyncio.run(
            run_swarm(USER.build(payload="Should we adopt demo?"), tools=tools, model=ScriptedLlm())
        )

    assert ended.value.reason == "did not pass CBAC verification"
    assert [s.agent.name for s in relay.sessions] == ["coordinator", "package_agent"]
    assert all(s.closed for s in relay.sessions)
