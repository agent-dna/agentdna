"""Tests for the AgentDNA Provenance Relay, without the network.

A fake signer stands in for AgentDNA and a small Starlette app for the
upstream MCP server, so every branch of the propagation policy can be driven
directly. The real thing - real signatures, a real FastMCP server with the
AgentDNA middleware, a real MCP client - is in test_relay_e2e.py.
"""

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentdna.error import RESULT_OK, TOOL_EXECUTION_FAILED
from agentdna.helpers import _hash_content
from agentdna.mcp.metadata import workflow_from_metadata, workflow_to_metadata
from agentdna.mcp.relay import ChainTerminated, ProvenanceRelay, WorkflowRejected
from agentdna.mcp.relay.app import create_app
from agentdna.mcp.relay.config import ConfigError, ProvenanceRelayConfig
from agentdna.mcp.relay.propagation import RELAY_ERROR
from agentdna.mcp.relay.service import (
    CallsInFlight,
    RunEnded,
    RunNotFound,
    WorkflowFailedVerification,
)
from agentdna.types import Envelope, IntentWorkflow

UPSTREAM_URL = "http://upstream/mcp"
VERIFICATION_FAILED = 2003
CBAC_DENIED = 3002


class FakeDNA:
    """Signs by naming itself; trusts every signer it has not been told to distrust."""

    distrusted: set[str] = set()

    def __init__(self, name: str) -> None:
        self.name = name
        self.recorded: list[IntentWorkflow] = []

    def get_actor_id(self) -> str:
        return f"did:{self.name}"

    def build(self, payload, previous_workflows=None, recipient_id="", verification_code=RESULT_OK):
        if isinstance(previous_workflows, IntentWorkflow):
            previous_workflows = [previous_workflows]
        previous_workflows = previous_workflows or []

        envelope = Envelope(
            from_=self.get_actor_id(),
            payload=payload,
            epoch=time.time_ns(),
            status_code=verification_code,
            to=recipient_id,
        )
        for previous in previous_workflows:
            envelope.add_envelope(previous.envelope)
        envelope.hash = _hash_content(envelope)
        envelope.signature = f"{self.get_actor_id()}|{envelope.hash}"

        workflow_id = previous_workflows[0].id if previous_workflows else "intent-1"
        return IntentWorkflow(
            id=workflow_id, type="intent_workflow", version="2.0", envelope=envelope
        )

    def verify(self, workflow, mode="boundary"):
        envelope = workflow.envelope
        signer, _, signed_hash = envelope.signature.partition("|")
        if signer in FakeDNA.distrusted or signed_hash != _hash_content(envelope):
            return VERIFICATION_FAILED
        return RESULT_OK

    def record(self, workflow):
        self.recorded.append(workflow)


@pytest.fixture(autouse=True)
def _reset_trust():
    FakeDNA.distrusted = set()
    yield
    FakeDNA.distrusted = set()


# -- the upstream MCP server ------------------------------------------------


class Upstream:
    """Answers tools/call the way an AgentDNA-protected server would - or wouldn't."""

    def __init__(self, sse: bool = False) -> None:
        self.dna = FakeDNA("server")
        self.sse = sse
        self.seen: list[dict] = []
        self.release = asyncio.Event()
        self.waiting = 0

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/mcp", self.handle, methods=["GET", "POST", "DELETE"])])

    async def handle(self, request: Request) -> Response:
        if request.method != "POST":
            return Response(f"{request.method} reached upstream", headers={"x-upstream": "yes"})

        message = json.loads(await request.body())
        self.seen.append(message)

        if message.get("method") != "tools/call":
            if "id" not in message:
                return Response(status_code=202)
            return JSONResponse(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message}},
                headers={"mcp-session-id": "session-1"},
            )

        params = message["params"]
        tool = params["name"]
        incoming = workflow_from_metadata(params.get("_meta"))

        if tool == "http_500":
            return Response("upstream broke", status_code=500)

        if tool == "wait":
            self.waiting += 1
            await self.release.wait()

        result = self.result(tool, incoming)
        if tool == "protocol_error":
            response = {"jsonrpc": "2.0", "id": message["id"], "error": result}
        else:
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}

        if not self.sse:
            return JSONResponse(response)

        progress = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": 1, "progress": 0.5},
        }
        body = (
            f"event: message\ndata: {json.dumps(progress)}\n\n"
            f"event: message\nid: 7\ndata: {json.dumps(response)}\n\n"
        )
        return Response(body, media_type="text/event-stream")

    def result(self, tool: str, incoming: IntentWorkflow | None) -> dict:
        content = [{"type": "text", "text": f"{tool} output"}]

        if tool == "protocol_error":
            return {"code": -32602, "message": "bad arguments"}
        if tool == "no_successor":
            return {"content": content, "isError": False}
        if tool == "refused":
            return {"content": [{"type": "text", "text": "not whitelisted"}], "isError": True}
        if tool == "input_required":
            return {"resultType": "input_required", "inputRequests": {}, "requestState": "s"}

        assert incoming is not None, "the relay must attach a workflow to every tools/call"

        if tool == "refused_with_successor":
            successor = self.dna.build(
                payload="denied", previous_workflows=incoming, verification_code=CBAC_DENIED
            )
            return {
                "content": [{"type": "text", "text": "denied by CBAC"}],
                "isError": True,
                "_meta": workflow_to_metadata(successor),
            }

        if tool == "tool_fails":
            successor = self.dna.build(
                payload="tool failed",
                previous_workflows=incoming,
                verification_code=TOOL_EXECUTION_FAILED,
            )
            return {"content": content, "isError": True, "_meta": workflow_to_metadata(successor)}

        if tool == "unrelated_successor":
            unrelated = FakeDNA("user").build(payload="some other intent")
            successor = self.dna.build(payload="ok", previous_workflows=unrelated)
        elif tool == "forged":
            successor = FakeDNA("mallory").build(payload="ok", previous_workflows=incoming)
        else:
            successor = self.dna.build(payload=f"{tool} done", previous_workflows=incoming)

        meta = workflow_to_metadata(successor)
        meta["vendor"] = {"kept": True}
        return {"content": content, "isError": False, "_meta": meta}


# -- harness ------------------------------------------------------------------


class Harness:
    def __init__(self, upstream: Upstream) -> None:
        self.user = FakeDNA("user")
        self.agent = FakeDNA("agent")
        self.upstream = upstream
        upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream.app()))
        config = ProvenanceRelayConfig(upstreams={"up": UPSTREAM_URL})
        self.app = create_app(config, upstream=upstream_client)
        self.service = self.app.state.service
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://relay"
        )
        self.next_id = 0

    async def open_run(self, workflows=None) -> str:
        workflows = workflows or [self.user.build(payload="summarise the repo")]
        run = await self.service.open(self.agent, workflows)
        return run.run_id

    async def call(self, run_id: str, tool: str, meta: dict | None = None) -> dict:
        self.next_id += 1
        params: dict = {"name": tool, "arguments": {"n": self.next_id}}
        if meta is not None:
            params["_meta"] = meta
        response = await self.client.post(
            f"/runs/{run_id}/mcp/up",
            json={"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call", "params": params},
            headers={"accept": "application/json, text/event-stream"},
        )
        return decode(response)

    async def frontier(self, run_id: str) -> list[IntentWorkflow]:
        return (await self.service.frontier(run_id)).context.get_workflows()

    async def in_flight(self, run_id: str) -> int:
        return (await self.service.frontier(run_id)).calls_in_flight


def decode(response: httpx.Response) -> dict:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [
            json.loads(line[len("data: ") :])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        return messages[-1]
    return response.json()


def payloads(workflows: list[IntentWorkflow]) -> list[str]:
    return [w.get_latest_envelope().payload for w in workflows]


def run(coro):
    return asyncio.run(coro)


# -- run lifecycle --------------------------------------------------------------


def test_open_run_keeps_the_workflow():
    async def scenario():
        h = Harness(Upstream())
        incoming = h.user.build(payload="summarise the repo")
        run_id = await h.open_run([incoming])
        frontier = await h.frontier(run_id)
        assert [w.get_latest_envelope().hash for w in frontier] == [incoming.envelope.hash]

    run(scenario())


def test_open_run_refuses_a_workflow_that_fails_verification():
    async def scenario():
        h = Harness(Upstream())
        FakeDNA.distrusted = {"did:user"}
        with pytest.raises(WorkflowFailedVerification) as raised:
            await h.open_run([h.user.build(payload="intent")])
        assert raised.value.details == {"status_code": VERIFICATION_FAILED}

    run(scenario())


def test_there_are_no_run_endpoints():
    """Runs are opened in memory only: nothing can open one over HTTP and have
    tool calls signed as an agent."""

    async def scenario():
        h = Harness(Upstream())
        workflow = h.user.build(payload="intent")
        body = {"workflows": [json.loads(workflow.serialize())]}
        assert (await h.client.post("/runs", json=body)).status_code == 404

        run_id = await h.open_run([workflow])
        assert (await h.client.get(f"/runs/{run_id}")).status_code == 404
        assert (await h.client.delete(f"/runs/{run_id}")).status_code == 404
        assert "result" in await h.call(run_id, "echo")

    run(scenario())


def test_unknown_run_is_refused_everywhere():
    async def scenario():
        h = Harness(Upstream())
        assert (await h.client.post("/runs/nope/mcp/up", json={})).status_code == 404
        with pytest.raises(RunNotFound):
            await h.service.frontier("nope")
        with pytest.raises(RunNotFound):
            await h.service.close("nope")
        assert h.upstream.seen == []

    run(scenario())


def test_closing_returns_the_final_frontier_and_forgets_the_run():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        await h.call(run_id, "echo")

        assert payloads(await h.service.close(run_id)) == ["echo done"]
        with pytest.raises(RunNotFound):
            await h.service.frontier(run_id)
        assert (await h.client.post(f"/runs/{run_id}/mcp/up", json={})).status_code == 404

    run(scenario())


def test_closing_with_a_call_in_flight_is_refused_unless_forced():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        call = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting == 0:
            await asyncio.sleep(0.01)

        with pytest.raises(CallsInFlight):
            await h.service.close(run_id)
        assert await h.in_flight(run_id) == 1

        h.upstream.release.set()
        await call
        assert payloads(await h.service.close(run_id)) == ["wait done"]

        # Forced, a close does not wait.
        run_id = await h.open_run()
        h.upstream.release.clear()
        call = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting < 2:
            await asyncio.sleep(0.01)
        assert payloads(await h.service.close(run_id, force=True)) == ["summarise the repo"]
        h.upstream.release.set()
        await call

    run(scenario())


def test_idle_runs_expire():
    async def scenario():
        h = Harness(Upstream())
        h.app.state.runs.ttl_seconds = 0.05
        run_id = await h.open_run()
        await asyncio.sleep(0.1)
        with pytest.raises(RunNotFound):
            await h.service.frontier(run_id)

    run(scenario())


# -- request side -------------------------------------------------------------------


def test_tool_call_carries_a_workflow_built_on_the_run_frontier():
    async def scenario():
        h = Harness(Upstream())
        incoming = h.user.build(payload="summarise the repo")
        run_id = await h.open_run([incoming])
        await h.call(run_id, "echo")

        sent = workflow_from_metadata(h.upstream.seen[0]["params"]["_meta"])
        assert sent is not None
        envelope = sent.get_latest_envelope()
        assert envelope.from_ == "did:agent"
        assert sent.id == incoming.id
        assert [p.hash for p in envelope.parent_envelope] == [incoming.envelope.hash]
        # The canonical tools/call payload (agentdna.mcp.payload).
        assert json.loads(envelope.payload) == {
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"n": 1}},
        }

    run(scenario())


def test_unrelated_meta_is_preserved_and_the_agentdna_namespace_is_owned():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        await h.call(
            run_id,
            "echo",
            meta={
                "progressToken": 42,
                "vendor": {"trace": "abc"},
                "agentdna": {"intent_workflow": "client-supplied", "extra": "kept"},
            },
        )

        meta = h.upstream.seen[0]["params"]["_meta"]
        assert meta["progressToken"] == 42
        assert meta["vendor"] == {"trace": "abc"}
        assert meta["agentdna"]["extra"] == "kept"
        assert meta["agentdna"]["intent_workflow"] != "client-supplied"
        assert workflow_from_metadata(meta).get_latest_envelope().from_ == "did:agent"

    run(scenario())


def test_other_requests_pass_through_untouched():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"a": 1}}

        response = await h.client.post(f"/runs/{run_id}/mcp/up", json=initialize)
        assert response.json()["result"]["echo"] == initialize
        assert response.headers["mcp-session-id"] == "session-1"

        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        response = await h.client.post(f"/runs/{run_id}/mcp/up", json=notification)
        assert response.status_code == 202

        response = await h.client.get(f"/runs/{run_id}/mcp/up")
        assert response.text == "GET reached upstream"
        response = await h.client.delete(f"/runs/{run_id}/mcp/up")
        assert response.text == "DELETE reached upstream"

        assert h.upstream.seen == [initialize, notification]

    run(scenario())


def test_batched_tool_calls_are_refused():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo"}},
        ]
        response = await h.client.post(f"/runs/{run_id}/mcp/up", json=batch)
        assert response.status_code == 400
        assert h.upstream.seen == []

    run(scenario())


def test_malformed_tool_call_is_refused_without_touching_the_frontier():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        response = await h.client.post(
            f"/runs/{run_id}/mcp/up",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ""}},
        )
        assert response.json()["error"]["code"] == RELAY_ERROR
        assert h.upstream.seen == []

        await h.call(run_id, "echo")
        assert payloads(await h.frontier(run_id)) == ["echo done"]

    run(scenario())


# -- response side ------------------------------------------------------------------


@pytest.mark.parametrize("sse", [False, True])
def test_verified_successor_advances_the_frontier(sse):
    async def scenario():
        h = Harness(Upstream(sse=sse))
        run_id = await h.open_run()

        first = await h.call(run_id, "first")
        assert first["result"]["content"][0]["text"] == "first output"
        assert first["result"]["_meta"]["vendor"] == {"kept": True}
        assert payloads(await h.frontier(run_id)) == ["first done"]

        await h.call(run_id, "second")
        frontier = await h.frontier(run_id)
        assert payloads(frontier) == ["second done"]

        # second -> agent request -> first -> agent request -> user intent
        chain = [e.payload for e in frontier[0].unwrap()]
        assert chain[0] == "second done"
        assert chain[2] == "first done"
        assert chain[-1] == "summarise the repo"

    run(scenario())


def test_sse_progress_events_pass_through():
    async def scenario():
        h = Harness(Upstream(sse=True))
        run_id = await h.open_run()
        response = await h.client.post(
            f"/runs/{run_id}/mcp/up",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo"}},
        )
        events = response.text.split("\n\n")
        assert "notifications/progress" in events[0]
        assert events[1].startswith("event: message\nid: 7\ndata: ")

    run(scenario())


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize(
    "tool, reason",
    [
        ("no_successor", "did not contain an AgentDNA successor"),
        ("unrelated_successor", "does not continue the workflow"),
    ],
)
def test_successful_results_without_a_valid_successor_are_withheld(sse, tool, reason):
    async def scenario():
        h = Harness(Upstream(sse=sse))
        run_id = await h.open_run()
        before = payloads(await h.frontier(run_id))

        answer = await h.call(run_id, tool)
        assert "result" not in answer
        assert answer["error"]["code"] == RELAY_ERROR
        assert reason in answer["error"]["message"]
        assert payloads(await h.frontier(run_id)) == before

    run(scenario())


def test_successor_failing_verification_is_withheld_and_recorded():
    async def scenario():
        h = Harness(Upstream())
        FakeDNA.distrusted = {"did:mallory"}
        run_id = await h.open_run()

        answer = await h.call(run_id, "forged")
        assert answer["error"]["data"] == {"agentdna_status_code": VERIFICATION_FAILED}
        assert payloads(await h.frontier(run_id)) == ["summarise the repo"]

        [recorded] = h.agent.recorded
        assert recorded.get_latest_envelope().status_code == VERIFICATION_FAILED

    run(scenario())


def test_tool_execution_failure_is_returned_and_kept_in_the_chain():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        answer = await h.call(run_id, "tool_fails")
        assert answer["result"]["isError"] is True
        [tip] = await h.frontier(run_id)
        assert tip.get_latest_envelope().status_code == TOOL_EXECUTION_FAILED

    run(scenario())


@pytest.mark.parametrize("tool", ["input_required"])
def test_results_without_a_successor_pass_through_and_leave_the_frontier(tool):
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        answer = await h.call(run_id, tool)
        assert answer == {"jsonrpc": "2.0", "id": 1, **_expected(tool)}
        assert payloads(await h.frontier(run_id)) == ["summarise the repo"]

        # The run is still usable.
        await h.call(run_id, "echo")
        assert payloads(await h.frontier(run_id)) == ["echo done"]

    def _expected(tool):
        result = Upstream().result(tool, None)
        return {"error": result} if tool == "protocol_error" else {"result": result}

    run(scenario())


# -- a chain that ends --------------------------------------------------------------


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize(
    ("tool", "reason", "status_code"),
    [
        # FastMCP 3: an error result without a successor.
        ("refused", "not whitelisted", None),
        # An error result whose successor is not a tool execution failure (4001).
        ("refused_with_successor", "denied by CBAC", CBAC_DENIED),
        # FastMCP 4: a JSON-RPC error.
        ("protocol_error", "bad arguments", None),
    ],
)
def test_a_refused_call_ends_the_run(sse, tool, reason, status_code):
    """The server recorded the refusal, so the chain ends: nothing more is called or returned."""

    async def scenario():
        h = Harness(Upstream(sse=sse))
        run_id = await h.open_run()

        # The agent sees the server's refusal as it was worded.
        answer = await h.call(run_id, tool)
        if "error" in answer:
            assert answer["error"]["message"] == reason
        else:
            assert answer["result"]["isError"] is True
            assert answer["result"]["content"][0]["text"] == reason

        # Every further tool call is refused without reaching the server.
        calls_seen = len(h.upstream.seen)
        refused = await h.call(run_id, "echo")
        assert refused["error"]["code"] == RELAY_ERROR
        assert "chain has ended" in refused["error"]["message"]
        assert len(h.upstream.seen) == calls_seen

        # Closing returns no frontier to build on, and forgets the run.
        with pytest.raises(RunEnded) as ended:
            await h.service.close(run_id)
        assert ended.value.details == {"reason": reason, "status_code": status_code}
        with pytest.raises(RunNotFound):
            await h.service.close(run_id)

    run(scenario())


def test_a_successor_failing_verification_ends_the_run():
    """The relay records the failed verification itself, so the chain ends there too."""

    async def scenario():
        h = Harness(Upstream())
        FakeDNA.distrusted = {"did:mallory"}
        run_id = await h.open_run()

        await h.call(run_id, "forged")
        refused = await h.call(run_id, "echo")
        assert "chain has ended" in refused["error"]["message"]

        with pytest.raises(RunEnded) as ended:
            await h.service.close(run_id)
        assert ended.value.details["status_code"] == VERIFICATION_FAILED

    run(scenario())


def test_an_ended_run_cannot_be_advanced():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        await h.call(run_id, "refused")

        [incoming] = await h.frontier(run_id)
        with pytest.raises(RunEnded):
            await h.service.advance(
                run_id, [h.agent.build(payload="hop", previous_workflows=incoming)]
            )

    run(scenario())


def test_an_ended_run_closes_even_with_a_call_in_flight():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        waiting = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting == 0:
            await asyncio.sleep(0.01)

        await h.call(run_id, "refused")
        with pytest.raises(RunEnded):
            await h.service.close(run_id)

        h.upstream.release.set()
        await waiting

    run(scenario())


def test_a_tool_execution_failure_does_not_end_the_run():
    """4001 is the one failure whose successor the server passes down: the chain goes on."""

    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        await h.call(run_id, "tool_fails")
        await h.call(run_id, "echo")
        assert payloads(await h.service.close(run_id)) == ["echo done"]

    run(scenario())


def test_upstream_http_error_passes_through_and_leaves_the_frontier():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        response = await h.client.post(
            f"/runs/{run_id}/mcp/up",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "http_500"},
            },
        )
        assert response.status_code == 500
        assert await h.in_flight(run_id) == 0

    run(scenario())


def test_unreachable_upstream_is_a_jsonrpc_error_and_leaves_the_frontier():
    async def scenario():
        h = Harness(Upstream())

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        h.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        run_id = await h.open_run()

        response = await h.client.post(
            f"/runs/{run_id}/mcp/up",
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo"}},
        )
        assert response.status_code == 502
        assert response.json()["id"] == 9
        assert response.json()["error"]["code"] == RELAY_ERROR
        assert await h.in_flight(run_id) == 0
        assert payloads(await h.frontier(run_id)) == ["summarise the repo"]

    run(scenario())


# -- concurrency ----------------------------------------------------------------------


def test_concurrent_calls_share_a_parent_and_merge_in_start_order():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        first = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting < 1:
            await asyncio.sleep(0.01)
        second = asyncio.create_task(h.call(run_id, "echo"))
        await second  # completes first, but started second
        assert payloads(await h.frontier(run_id)) == ["summarise the repo"]

        h.upstream.release.set()
        await first

        assert payloads(await h.frontier(run_id)) == ["wait done", "echo done"]
        sent = [workflow_from_metadata(m["params"]["_meta"]) for m in h.upstream.seen]
        parents = {e.hash for w in sent for e in w.get_latest_envelope().parent_envelope}
        assert len(parents) == 1  # both built on the same frontier

    run(scenario())


def test_a_failed_concurrent_call_does_not_lose_the_successful_one():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        good = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting < 1:
            await asyncio.sleep(0.01)
        bad = await h.call(run_id, "no_successor")
        assert "error" in bad

        h.upstream.release.set()
        await good
        assert payloads(await h.frontier(run_id)) == ["wait done"]

        await h.call(run_id, "echo")
        [tip] = await h.frontier(run_id)
        assert tip.get_latest_envelope().payload == "echo done"
        assert "wait done" in [e.payload for e in tip.unwrap()]

    run(scenario())


def test_runs_are_isolated():
    async def scenario():
        h = Harness(Upstream())
        run_a = await h.open_run([h.user.build(payload="intent A")])
        run_b = await h.open_run([h.user.build(payload="intent B")])

        await h.call(run_a, "echo")
        assert payloads(await h.frontier(run_a)) == ["echo done"]
        assert payloads(await h.frontier(run_b)) == ["intent B"]

    run(scenario())


# -- configuration -------------------------------------------------------------------


@pytest.mark.parametrize(
    "servers",
    [
        {},
        {"x": "ftp://x/mcp"},
        {"x": "not a url"},
        {"a/b": "http://x/mcp"},
        {"a b": "http://x/mcp"},
    ],
)
def test_invalid_servers_are_refused(servers):
    with pytest.raises(ConfigError):
        ProvenanceRelayConfig(upstreams=servers)
    with pytest.raises(ConfigError):
        ProvenanceRelay(servers)


@pytest.mark.parametrize(
    "field, value",
    [("port", -1), ("port", 65536), ("run_ttl_seconds", 0), ("upstream_timeout_seconds", 0)],
)
def test_invalid_relay_settings_are_refused(field, value):
    with pytest.raises(ConfigError):
        ProvenanceRelayConfig(upstreams={"x": "http://x/mcp"}, **{field: value})


def test_config_defaults():
    config = ProvenanceRelayConfig(upstreams={"github": "http://127.0.0.1:8013/mcp"})
    assert (config.host, config.port) == ("127.0.0.1", 0)  # loopback, OS-chosen port


# -- several MCP servers ---------------------------------------------------------------


def test_one_run_spans_several_servers_with_one_frontier():
    async def scenario():
        github, jira = Upstream(), Upstream()
        jira.dna = FakeDNA("jira-server")
        agent = FakeDNA("agent")
        config = ProvenanceRelayConfig(
            upstreams={"github": "http://github/mcp", "jira": "http://jira/mcp"}
        )

        class Router(httpx.AsyncBaseTransport):
            def __init__(self):
                self.transports = {
                    "github": httpx.ASGITransport(app=github.app()),
                    "jira": httpx.ASGITransport(app=jira.app()),
                }

            async def handle_async_request(self, request):
                return await self.transports[request.url.host].handle_async_request(request)

        app = create_app(config, upstream=httpx.AsyncClient(transport=Router()))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")

        incoming = FakeDNA("user").build(payload="triage")
        service = app.state.service
        assert service.servers == ["github", "jira"]
        run_id = (await service.open(agent, [incoming])).run_id

        def call(server, tool, call_id):
            return client.post(
                f"/runs/{run_id}/mcp/{server}",
                json={
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": tool},
                },
            )

        assert "result" in (await call("github", "commits", 1)).json()
        assert "result" in (await call("jira", "tickets", 2)).json()
        assert [m["params"]["name"] for m in github.seen] == ["commits"]
        assert [m["params"]["name"] for m in jira.seen] == ["tickets"]

        # The jira call was built on the github call's successor.
        [tip] = (await service.frontier(run_id)).context.get_workflows()
        assert tip.get_latest_envelope().payload == "tickets done"
        assert "commits done" in [e.payload for e in tip.unwrap()]

        response = await call("nope", "commits", 3)
        assert response.status_code == 404

    run(scenario())


# -- the session API, on a real in-process relay -------------------------------------


def _serve(app) -> tuple[str, Any]:
    import socket
    import threading

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    return f"http://127.0.0.1:{port}", server


@pytest.fixture
def served_upstream():
    upstream = Upstream()
    url, server = _serve(upstream.app())
    yield upstream, f"{url}/mcp"
    server.should_exit = True


@pytest.fixture
def relay(served_upstream):
    _, url = served_upstream
    relay = ProvenanceRelay({"github": url})
    yield relay
    relay.close()


async def _tool_call(url: str, tool: str = "echo") -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool}},
        )
    return response.json()


def test_session_returns_the_final_workflows(relay):
    agent = FakeDNA("agent")

    async def scenario():
        incoming = FakeDNA("user").build(payload="summarise")
        async with relay.session(agent, incoming) as session:
            assert session.agent is agent
            assert session.servers == ["github"]
            assert session.url() == session.url("github")
            assert session.url("github").endswith(f"/runs/{session.run_id}/mcp/github")
            with pytest.raises(KeyError):
                session.url("jira")
            with pytest.raises(RuntimeError, match="still open"):
                session.workflows  # noqa: B018 - not final while open

            assert "result" in await _tool_call(session.url("github"))

        assert session.closed
        [final] = session.workflows
        assert final.get_latest_envelope().payload == "echo done"
        assert final.id == incoming.id
        assert incoming.envelope.hash in [e.hash for e in final.unwrap()]

        reply = agent.build(payload="the summary", previous_workflows=session.workflows)
        assert [p.payload for p in reply.get_latest_envelope().parent_envelope] == ["echo done"]

    run(scenario())


def test_one_relay_signs_as_each_sessions_agent(relay, served_upstream):
    """No identity of its own: each session signs as the agent that opened it."""

    upstream, _ = served_upstream
    orchestrator, worker = FakeDNA("orchestrator"), FakeDNA("worker")

    async def scenario():
        async with relay.session(orchestrator, FakeDNA("user").build(payload="a")) as first:
            async with relay.session(worker, FakeDNA("user").build(payload="b")) as second:
                await _tool_call(first.url())
                await _tool_call(second.url())

        signers = [
            workflow_from_metadata(m["params"]["_meta"]).get_latest_envelope().from_
            for m in upstream.seen
        ]
        assert signers == ["did:orchestrator", "did:worker"]
        assert first.workflows[0].get_latest_envelope().payload == "echo done"
        assert second.workflows[0].get_latest_envelope().payload == "echo done"

    run(scenario())


def test_session_rejects_an_unverified_workflow(relay):
    FakeDNA.distrusted = {"did:user"}

    async def scenario():
        with pytest.raises(WorkflowRejected) as raised:
            async with relay.session(FakeDNA("agent"), FakeDNA("user").build(payload="x")):
                pass
        assert raised.value.status_code == VERIFICATION_FAILED

    run(scenario())


def test_session_from_synchronous_code(relay):
    incoming = FakeDNA("user").build(payload="x")
    with relay.session(FakeDNA("agent"), incoming) as session:
        assert [w.get_latest_envelope().hash for w in session.frontier()] == [
            incoming.envelope.hash
        ]
        workflows = session.close()
    assert [w.get_latest_envelope().hash for w in workflows] == [incoming.envelope.hash]
    assert [w.get_latest_envelope().hash for w in session.workflows] == [incoming.envelope.hash]


def test_close_inside_the_block_returns_the_final_workflows(relay):
    async def scenario():
        incoming = FakeDNA("user").build(payload="x")
        async with relay.session(FakeDNA("agent"), incoming) as session:
            workflows = await session.aclose()
        assert [w.get_latest_envelope().hash for w in workflows] == [incoming.envelope.hash]

    run(scenario())


def test_session_is_force_closed_when_the_block_raises(relay):
    async def scenario():
        with pytest.raises(RuntimeError, match="agent blew up"):
            async with relay.session(
                FakeDNA("agent"), FakeDNA("user").build(payload="x")
            ) as session:
                raise RuntimeError("agent blew up")
        assert session.closed

    run(scenario())


def test_relays_get_their_own_os_chosen_ports():
    """One port per relay, however many sessions; never a fixed port that a
    second agent process would collide with."""

    first = ProvenanceRelay({"up": UPSTREAM_URL})
    second = ProvenanceRelay({"up": UPSTREAM_URL})
    try:
        bases = set()
        for relay in (first, second, first, second):
            with relay.session(FakeDNA("agent"), FakeDNA("user").build(payload="x")) as session:
                bases.add(session.url().rsplit("/runs/", 1)[0])
        assert len(bases) == 2  # one per relay, reused across its sessions
    finally:
        first.close()
        second.close()


# -- advancing a session's frontier with hops made outside MCP -------------------------


def test_advance_moves_the_frontier_and_the_next_call_builds_on_it():
    async def scenario():
        h = Harness(Upstream())
        incoming = h.user.build(payload="plan a trip")
        run_id = await h.open_run([incoming])

        hop = h.agent.build(payload="handoff", previous_workflows=[incoming])
        assert payloads(await h.service.advance(run_id, [hop])) == ["handoff"]

        await h.call(run_id, "echo")
        sent = workflow_from_metadata(h.upstream.seen[0]["params"]["_meta"])
        assert [p.hash for p in sent.get_latest_envelope().parent_envelope] == [hop.envelope.hash]
        assert payloads(await h.frontier(run_id)) == ["echo done"]

    run(scenario())


def test_advance_must_build_on_the_current_frontier():
    from agentdna.mcp.relay.service import NotAContinuation

    async def scenario():
        h = Harness(Upstream())
        incoming = h.user.build(payload="plan a trip")
        run_id = await h.open_run([incoming])

        unrelated = h.agent.build(
            payload="elsewhere", previous_workflows=[h.user.build(payload="x")]
        )
        with pytest.raises(NotAContinuation):
            await h.service.advance(run_id, [unrelated])
        with pytest.raises(NotAContinuation):
            await h.service.advance(run_id, [])
        assert [w.envelope.hash for w in await h.frontier(run_id)] == [incoming.envelope.hash]

    run(scenario())


def test_advance_is_refused_while_a_call_is_in_flight():
    async def scenario():
        h = Harness(Upstream())
        incoming = h.user.build(payload="plan a trip")
        run_id = await h.open_run([incoming])

        call = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting == 0:
            await asyncio.sleep(0.01)
        with pytest.raises(CallsInFlight):
            await h.service.advance(
                run_id, [h.agent.build(payload="hop", previous_workflows=[incoming])]
            )

        h.upstream.release.set()
        await call

    run(scenario())


def test_a_handoff_between_two_agents_joins_their_chains(relay, served_upstream):
    """The swarm pattern: agent A hands off to agent B by signing a hop on both
    their frontiers; B's session advances to it and B's tool calls build on it."""

    from agentdna.mcp.relay import RelayError

    upstream, _ = served_upstream
    a, b = FakeDNA("agent-a"), FakeDNA("agent-b")
    incoming = FakeDNA("user").build(payload="plan a trip")

    async def scenario():
        async with relay.session(a, incoming) as sa, relay.session(b, incoming) as sb:
            await _tool_call(sa.url())  # A works first

            hop = a.build(
                payload="handoff to agent-b",
                previous_workflows=[*await sa.afrontier(), *await sb.afrontier()],
                recipient_id=b.get_actor_id(),
            )
            await sb.aadvance(hop)
            await _tool_call(sb.url())  # then B, on top of the hand-off

            with pytest.raises(RelayError) as refused:
                await sb.aadvance(a.build(payload="stale", previous_workflows=[incoming]))
            assert refused.value.status == 400

        return sa, sb

    sa, sb = run(scenario())
    signers = [
        workflow_from_metadata(m["params"]["_meta"]).get_latest_envelope().from_
        for m in upstream.seen
    ]
    assert signers == ["did:agent-a", "did:agent-b"]

    # B's final chain runs: user -> A's call -> A's hand-off -> B's call.
    [tip] = sb.workflows
    chain = [e.payload for e in tip.unwrap()]
    assert "handoff to agent-b" in chain
    assert sum(p == "echo done" for p in chain) == 2
    assert chain[-1] == "plan a trip"


def test_a_session_whose_chain_ended_raises_on_close(relay):
    agent = FakeDNA("agent")

    async def scenario():
        with pytest.raises(ChainTerminated) as ended:
            async with relay.session(agent, FakeDNA("user").build(payload="x")) as session:
                refusal = await _tool_call(session.url(), "refused")
                assert refusal["result"]["isError"] is True
                refused = await _tool_call(session.url(), "echo")
                assert "chain has ended" in refused["error"]["message"]

        assert ended.value.reason == "not whitelisted"
        assert ended.value.status == 410
        # Closed, with nothing to build on: reading the workflows raises the same.
        assert session.closed
        with pytest.raises(ChainTerminated):
            session.workflows  # noqa: B018
        with pytest.raises(ChainTerminated):
            await session.aclose()

    run(scenario())


def test_a_sync_session_whose_chain_ended_raises_on_close(relay):
    async def refuse(url):
        return await _tool_call(url, "refused")

    with pytest.raises(ChainTerminated):
        with relay.session(FakeDNA("agent"), FakeDNA("user").build(payload="x")) as session:
            asyncio.run(refuse(session.url()))
