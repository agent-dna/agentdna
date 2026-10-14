"""The swarm's provenance DAG for a recorded run: no network, no keys.

Replays the run in examples/adk_travel_swarm/envelope.json - coordinator ->
destination_agent (two parallel tool calls) -> weather_agent -> money_agent ->
coordinator - through the real HandoffRecorder, with stand-in identities and
sessions, and checks the chain records exactly that flow: the user deals only
with the coordinator, and every specialist is reached through a hand-off.
"""

import asyncio
import json
import sys
import time
import types

import pytest

from agentdna.helpers import _hash_content
from agentdna.types import Envelope, IntentWorkflow

AGENTS = ("coordinator", "weather_agent", "destination_agent", "money_agent")


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
    """A gateway session's frontier, with the gateway's rules for tool calls and advance()."""

    def __init__(self, agent: FakeIdentity, incoming: IntentWorkflow) -> None:
        self.agent = agent
        self.frontier = [incoming]

    async def afrontier(self):
        return list(self.frontier)

    async def aadvance(self, hop: IntentWorkflow) -> None:
        reachable = {(e.hash, e.signature) for e in hop.unwrap()}
        for workflow in self.frontier:
            tip = workflow.get_latest_envelope()
            assert (tip.hash, tip.signature) in reachable, "advance() must extend the chain"
        self.frontier = [hop]

    def calls(self, *tools: str) -> None:
        """Tool calls made together: each signed on the same frontier, each answered."""
        results = []
        for tool in tools:
            request = self.agent.build(
                payload=json.dumps({"method": "tools/call", "params": {"name": tool}}),
                previous_workflows=self.frontier,
            )
            results.append(SERVER.build(payload=f"{tool} result", previous_workflows=request))
        self.frontier = results


@pytest.fixture
def swarm(monkeypatch):
    # swarm.py imports the real identities, which register on the provenance layer.
    identities = {name: FakeIdentity(name) for name in AGENTS}
    monkeypatch.setitem(sys.modules, "identities", types.SimpleNamespace(IDENTITIES=identities))
    monkeypatch.delitem(sys.modules, "swarm", raising=False)
    import swarm

    return swarm, identities


def replay_recorded_run(swarm, identities):
    incoming = USER.build(
        payload="Flying from New York to Lisbon: weather, sights, 500 USD in EUR?"
    )
    sessions = {name: FakeSession(identities[name], incoming) for name in AGENTS}
    recorder = swarm.HandoffRecorder(sessions)

    async def take_control(agent: str) -> None:
        await recorder.before_agent(callback_context=types.SimpleNamespace(agent_name=agent))

    async def run():
        await take_control("coordinator")
        sessions["coordinator"].calls("geocode")
        await take_control("destination_agent")
        sessions["destination_agent"].calls("wikipedia_summary", "local_time")
        await take_control("weather_agent")
        sessions["weather_agent"].calls("weather_forecast")
        await take_control("money_agent")
        sessions["money_agent"].calls("convert_currency")
        await take_control("coordinator")

    asyncio.run(run())
    tips = swarm._tips([w for session in sessions.values() for w in session.frontier])
    final = identities["coordinator"].build(payload="travel brief", previous_workflows=tips)
    return incoming, recorder, final


def children_of(final: IntentWorkflow, parent: Envelope) -> list[str]:
    return sorted(
        e.payload
        for e in final.unwrap()
        if any(p.hash == parent.hash for p in e.parent_envelope or [])
    )


def parents_of(final: IntentWorkflow, payload: str) -> list[str]:
    [envelope] = [e for e in final.unwrap() if e.payload == payload]
    return sorted(p.payload for p in envelope.parent_envelope or [])


def handoff(source: str, target: str) -> str:
    return json.dumps({"type": "handoff", "from": source, "to": target}, sort_keys=True)


def test_the_user_deals_only_with_the_coordinator(swarm):
    incoming, _, final = replay_recorded_run(*swarm)
    request = json.dumps({"method": "tools/call", "params": {"name": "geocode"}})
    assert children_of(final, incoming.envelope) == [request]


def test_every_link_is_one_that_happened(swarm):
    _, recorder, final = replay_recorded_run(*swarm)

    assert [(h.source, h.target) for h in recorder.handoffs] == [
        ("coordinator", "destination_agent"),
        ("destination_agent", "weather_agent"),
        ("weather_agent", "money_agent"),
        ("money_agent", "coordinator"),
    ]
    assert parents_of(final, handoff("coordinator", "destination_agent")) == ["geocode result"]
    assert parents_of(final, handoff("destination_agent", "weather_agent")) == [
        "local_time result",
        "wikipedia_summary result",
    ]
    assert parents_of(final, handoff("weather_agent", "money_agent")) == ["weather_forecast result"]
    assert parents_of(final, handoff("money_agent", "coordinator")) == ["convert_currency result"]
    assert parents_of(final, "travel brief") == [handoff("money_agent", "coordinator")]


def test_nothing_that_happened_is_missing(swarm):
    _, _, final = replay_recorded_run(*swarm)
    # intent + 5 tool calls + 5 results + 4 hand-offs + the brief
    assert len(final.unwrap()) == 16


def test_tips_keep_independent_work_and_drop_ancestors(swarm):
    module, identities = swarm
    a, b = identities["weather_agent"], identities["money_agent"]
    root = USER.build(payload="root")
    left = a.build(payload="left", previous_workflows=root)
    right = b.build(payload="right", previous_workflows=root)

    assert [w.envelope.payload for w in module._tips([left, root, right, left])] == [
        "left",
        "right",
    ]
