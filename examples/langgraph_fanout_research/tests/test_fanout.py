"""The fan-out: no network, no keys.

Runs the real LangGraph graph - plan, Send to parallel workers, synthesize -
with a scripted LLM and scripted workers, stand-in AgentDNA identities and a
stand-in Provenance Relay, and checks that one worker is spawned per subtask,
that every worker reports, that the final answer is synthesized from all the
reports, and that the signed chain records the fan-out: one sub-agent and one
delegation per worker, each report signed by its worker, the answer on all of
them.
"""

import asyncio
import dataclasses
import json
import sys
import time
import types

import pytest
from langchain_core.messages import AIMessage

from agentdna.helpers import _hash_content
from agentdna.mcp.relay import ChainTerminated
from agentdna.types import Envelope, IntentWorkflow


class FakeIdentity:
    """Signs by naming itself; enough to give every envelope a unique identity."""

    def __init__(self, name: str, verify_code: int = 1000) -> None:
        self.name = name
        self.verify_code = verify_code
        self.recorded: list[IntentWorkflow] = []

    def get_actor_id(self) -> str:
        return f"did:{self.name}"

    def verify(self, workflow, mode="boundary"):
        return self.verify_code

    def record(self, workflow):
        self.recorded.append(workflow)

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


class FakeSession:
    """A relay session: closes with the workflow it received, or as a refusal ended it."""

    def __init__(self, agent, received, refuse: bool) -> None:
        self.agent, self.received, self.refuse = agent, received, refuse
        self.workflows = [received]

    def url(self, server: str) -> str:
        assert server == "research"
        return "fake://research"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if self.refuse and exc[0] is None:
            raise ChainTerminated(410, {"reason": "not whitelisted"})


class FakeRelay:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.refuse_for: set[str] = set()  # subtask instructions whose tool calls are refused

    def session(self, agent, workflows):
        subtask = json.loads(workflows.envelope.payload)["subtask"]["instructions"]
        session = FakeSession(agent, workflows, subtask in self.refuse_for)
        self.sessions.append(session)
        return session


class FakeAgent:
    """A ReAct worker that answers from its instructions, or fails if told to."""

    def __init__(self, spawned: list[str]) -> None:
        self.spawned = spawned

    async def ainvoke(self, state):
        instructions = state["messages"][0].content
        self.spawned.append(instructions)
        await asyncio.sleep(0)  # let the other workers interleave
        if "fail" in instructions:
            raise RuntimeError("Gemini quota exceeded")
        return {"messages": [AIMessage(f"findings: {instructions}")]}


class FakeLLM:
    def __init__(self, subtasks: list[str]) -> None:
        self.subtasks = subtasks
        self.briefings: list[str] = []

    def with_structured_output(self, schema, method=None):
        subtasks = [{"title": s, "instructions": s} for s in self.subtasks]

        async def ainvoke(messages):
            return schema(subtasks=subtasks)

        return types.SimpleNamespace(ainvoke=ainvoke)

    async def ainvoke(self, messages):
        self.briefings.append(messages[-1].content)
        return AIMessage("the answer")


USER = FakeIdentity("user")


@pytest.fixture
def fanout(monkeypatch):
    """graph.py, wired to the fakes instead of Gemini, the MCP server and AgentDNA."""

    async def load_tools(mcp_url):
        assert mcp_url == "fake://research"
        return []

    llm, spawned, relay = FakeLLM([]), [], FakeRelay()
    orchestrator, workers = FakeIdentity("orchestrator"), []

    def spawn_worker():
        workers.append(FakeIdentity(f"orchestrator-sub{len(workers) + 1}"))
        return workers[-1]

    monkeypatch.setitem(
        sys.modules,
        "identities",
        types.SimpleNamespace(ORCHESTRATOR=orchestrator, spawn_worker=spawn_worker),
    )
    monkeypatch.setitem(
        sys.modules,
        "mcp_client",
        types.SimpleNamespace(PROVENANCE_RELAY=relay, load_tools=load_tools),
    )
    monkeypatch.setitem(sys.modules, "llm", types.SimpleNamespace(build_llm=lambda: llm))
    monkeypatch.delitem(sys.modules, "graph", raising=False)
    import graph

    monkeypatch.setattr(
        graph, "create_react_agent", lambda model, tools, prompt: FakeAgent(spawned)
    )
    graph.fakes = types.SimpleNamespace(orchestrator=orchestrator, workers=workers, relay=relay)
    return graph, llm, spawned


def run(graph, question: str):
    return asyncio.run(graph.run_fanout(USER.build(payload=question)))


def test_one_worker_per_subtask(fanout):
    graph, llm, spawned = fanout
    llm.subtasks = ["Kenya", "Germany", "India"]
    result = run(graph, "Compare Kenya, Germany and India")

    assert sorted(spawned) == ["Germany", "India", "Kenya"]
    assert [(r.instance, r.title, r.ok) for r in result.reports] == [
        ("worker-1", "Kenya", True),
        ("worker-2", "Germany", True),
        ("worker-3", "India", True),
    ]


def test_the_answer_is_synthesized_from_every_report(fanout):
    graph, llm, _ = fanout
    llm.subtasks = ["Kenya", "Germany", "India"]
    result = run(graph, "Compare Kenya, Germany and India")

    assert result.result == "the answer"
    [briefing] = llm.briefings
    assert briefing.startswith("Question: Compare Kenya, Germany and India")
    for title in ["Kenya", "Germany", "India"]:
        assert f"findings: {title}" in briefing


def test_a_failed_worker_still_reports(fanout):
    graph, llm, _ = fanout
    llm.subtasks = ["Kenya", "fail on Germany", "India"]
    result = run(graph, "Compare Kenya, Germany and India")

    assert [(r.instance, r.ok) for r in result.reports] == [
        ("worker-1", True),
        ("worker-2", False),
        ("worker-3", True),
    ]
    assert "Gemini quota exceeded" in result.reports[1].findings
    assert "worker-2: fail on Germany (failed)" in llm.briefings[0]


def test_an_empty_plan_falls_back_to_one_worker(fanout):
    graph, llm, spawned = fanout
    llm.subtasks = []
    result = run(graph, "What is the capital of Kenya?")

    assert spawned == ["What is the capital of Kenya?"]
    assert [r.instance for r in result.reports] == ["worker-1"]


def test_the_orchestrator_spawns_at_most_max_workers(fanout, monkeypatch):
    graph, llm, spawned = fanout
    monkeypatch.setattr(graph, "settings", dataclasses.replace(graph.settings, max_workers=2))
    llm.subtasks = ["a", "b", "c", "d"]
    result = run(graph, "Too many subtasks")

    assert [r.instance for r in result.reports] == ["worker-1", "worker-2"]
    assert sorted(spawned) == ["a", "b"]


# -- the chain of custody --------------------------------------------------------------


def payload(workflow: IntentWorkflow) -> dict:
    return json.loads(workflow.envelope.payload)


def test_each_worker_is_a_new_sub_agent_with_its_own_delegation(fanout):
    graph, llm, _ = fanout
    llm.subtasks = ["Kenya", "Germany", "India"]
    run(graph, "Compare Kenya, Germany and India")

    workers, sessions = graph.fakes.workers, graph.fakes.relay.sessions
    assert len({w.get_actor_id() for w in workers}) == 3
    # Each worker's session is opened as that worker, on a delegation addressed to it.
    assert {s.agent.get_actor_id() for s in sessions} == {w.get_actor_id() for w in workers}
    for session in sessions:
        delegation = session.received.envelope
        assert delegation.from_ == "did:orchestrator"
        assert delegation.to == session.agent.get_actor_id()
        assert [p.payload for p in delegation.parent_envelope] == [
            "Compare Kenya, Germany and India"
        ]


def test_reports_are_signed_by_their_workers_and_the_answer_joins_them(fanout):
    graph, llm, _ = fanout
    llm.subtasks = ["Kenya", "Germany", "India"]
    result = run(graph, "Compare Kenya, Germany and India")

    for report in result.reports:
        envelope = report.adna_workflow.envelope
        assert envelope.to == "did:orchestrator"
        assert payload(report.adna_workflow)["instance"] == report.instance
        [delegation] = envelope.parent_envelope
        assert delegation.to == envelope.from_  # the worker that signed is the one delegated to

    final = result.adna_workflow.envelope
    assert (final.from_, final.payload) == ("did:orchestrator", "the answer")
    assert sorted(json.loads(p.payload)["instance"] for p in final.parent_envelope) == [
        "worker-1",
        "worker-2",
        "worker-3",
    ]


def test_a_failed_worker_signs_its_failure_on_its_delegation(fanout):
    graph, llm, _ = fanout
    llm.subtasks = ["Kenya", "fail on Germany"]
    result = run(graph, "Compare Kenya and Germany")

    failed = payload(result.reports[1].adna_workflow)
    assert (failed["instance"], failed["ok"]) == ("worker-2", False)
    assert [
        json.loads(p.payload)["instance"]
        for p in result.reports[1].adna_workflow.envelope.parent_envelope
    ] == ["worker-2"]


def test_a_refused_tool_call_stops_the_run(fanout):
    graph, llm, _ = fanout
    llm.subtasks = ["Kenya", "Germany"]
    graph.fakes.relay.refuse_for = {"Germany"}

    with pytest.raises(ChainTerminated):
        run(graph, "Compare Kenya and Germany")


def test_a_request_that_fails_verification_is_recorded_and_refused(fanout):
    graph, llm, spawned = fanout
    graph.fakes.orchestrator.verify_code = 2003
    llm.subtasks = ["Kenya"]

    with pytest.raises(RuntimeError, match="failed AgentDNA verification with code 2003"):
        run(graph, "Tell me about Kenya")

    [recorded] = graph.fakes.orchestrator.recorded
    assert recorded.envelope.status_code == 2003
    assert spawned == []  # no worker was spawned
