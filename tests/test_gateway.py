"""Tests for the AgentDNA MCP Gateway, without the network.

A fake signer stands in for AgentDNA and a small Starlette app for the
upstream MCP server, so every branch of the propagation policy can be driven
directly. The real thing - real signatures, a real FastMCP server with the
AgentDNA middleware, a real MCP client - is in test_gateway_e2e.py.
"""

import asyncio
import json
import time

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentdna.error import RESULT_OK, TOOL_EXECUTION_FAILED
from agentdna.helpers import _hash_content
from agentdna.mcp.gateway.app import create_app
from agentdna.mcp.gateway.config import (
    AgentIdentityConfig,
    ConfigError,
    GatewayConfig,
)
from agentdna.mcp.gateway.propagation import AGENTDNA_GATEWAY_ERROR
from agentdna.mcp.metadata import workflow_from_metadata, workflow_to_metadata
from agentdna.types import Envelope, IntentWorkflow, load_workflow

UPSTREAM_URL = "http://upstream/mcp"
VERIFICATION_FAILED = 2003


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

        assert incoming is not None, "the gateway must attach a workflow to every tools/call"

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
    def __init__(self, upstream: Upstream, token: str = "") -> None:
        self.user = FakeDNA("user")
        self.agent = FakeDNA("agent")
        self.upstream = upstream
        upstream_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream.app()))
        config = GatewayConfig(upstream_url=UPSTREAM_URL, token=token)
        self.app = create_app(self.agent, config, upstream=upstream_client)  # type: ignore[arg-type]
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://gateway"
        )
        self.next_id = 0

    async def open_run(self, workflows=None, headers=None) -> str:
        workflows = workflows or [self.user.build(payload="summarise the repo")]
        response = await self.client.post(
            "/runs",
            json={"workflows": [json.loads(w.serialize()) for w in workflows]},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["run_id"]

    async def call(self, run_id: str, tool: str, meta: dict | None = None) -> dict:
        self.next_id += 1
        params: dict = {"name": tool, "arguments": {"n": self.next_id}}
        if meta is not None:
            params["_meta"] = meta
        response = await self.client.post(
            f"/runs/{run_id}/mcp",
            json={"jsonrpc": "2.0", "id": self.next_id, "method": "tools/call", "params": params},
            headers={"accept": "application/json, text/event-stream"},
        )
        return decode(response)

    async def frontier(self, run_id: str) -> list[IntentWorkflow]:
        response = await self.client.get(f"/runs/{run_id}")
        assert response.status_code == 200, response.text
        return [load_workflow(w) for w in response.json()["workflows"]]


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


def test_open_run_returns_an_mcp_path_and_keeps_the_workflow():
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
        workflow = h.user.build(payload="intent")
        response = await h.client.post(
            "/runs", json={"workflows": [json.loads(workflow.serialize())]}
        )
        assert response.status_code == 422
        assert response.json()["status_code"] == VERIFICATION_FAILED

    run(scenario())


@pytest.mark.parametrize(
    "body",
    [{}, {"workflows": []}, {"workflows": ["not json"]}, {"workflows": [{"id": "x"}]}, []],
)
def test_open_run_rejects_malformed_requests(body):
    async def scenario():
        h = Harness(Upstream())
        response = await h.client.post("/runs", json=body)
        assert response.status_code == 400

    run(scenario())


def test_run_endpoints_require_the_token_when_configured():
    async def scenario():
        h = Harness(Upstream(), token="s3cret")
        workflow = h.user.build(payload="intent")
        body = {"workflows": [json.loads(workflow.serialize())]}

        assert (await h.client.post("/runs", json=body)).status_code == 401
        wrong = {"authorization": "Bearer nope"}
        assert (await h.client.post("/runs", json=body, headers=wrong)).status_code == 401

        run_id = await h.open_run([workflow], headers={"authorization": "Bearer s3cret"})
        assert (await h.client.get(f"/runs/{run_id}")).status_code == 401
        assert (await h.client.delete(f"/runs/{run_id}")).status_code == 401

    run(scenario())


def test_unknown_run_is_404_on_every_endpoint():
    async def scenario():
        h = Harness(Upstream())
        assert (await h.client.get("/runs/nope")).status_code == 404
        assert (await h.client.delete("/runs/nope")).status_code == 404
        assert (await h.client.post("/runs/nope/mcp", json={})).status_code == 404
        assert h.upstream.seen == []

    run(scenario())


def test_closing_returns_the_final_frontier_and_forgets_the_run():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        await h.call(run_id, "echo")

        response = await h.client.delete(f"/runs/{run_id}")
        assert response.status_code == 200
        assert payloads([load_workflow(w) for w in response.json()["workflows"]]) == ["echo done"]
        assert (await h.client.get(f"/runs/{run_id}")).status_code == 404

    run(scenario())


def test_closing_with_a_call_in_flight_is_refused_unless_forced():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()

        call = asyncio.create_task(h.call(run_id, "wait"))
        while h.upstream.waiting == 0:
            await asyncio.sleep(0.01)

        response = await h.client.delete(f"/runs/{run_id}")
        assert response.status_code == 409
        assert (await h.client.get(f"/runs/{run_id}")).json()["calls_in_flight"] == 1

        h.upstream.release.set()
        await call
        assert (await h.client.delete(f"/runs/{run_id}")).status_code == 200

    run(scenario())


def test_idle_runs_expire():
    async def scenario():
        h = Harness(Upstream())
        h.app.state.runs.ttl_seconds = 0.05
        run_id = await h.open_run()
        await asyncio.sleep(0.1)
        assert (await h.client.get(f"/runs/{run_id}")).status_code == 404

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
        # Same canonical payload as the in-process client adapters.
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

        response = await h.client.post(f"/runs/{run_id}/mcp", json=initialize)
        assert response.json()["result"]["echo"] == initialize
        assert response.headers["mcp-session-id"] == "session-1"

        notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        response = await h.client.post(f"/runs/{run_id}/mcp", json=notification)
        assert response.status_code == 202

        response = await h.client.get(f"/runs/{run_id}/mcp")
        assert response.text == "GET reached upstream"
        response = await h.client.delete(f"/runs/{run_id}/mcp")
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
        response = await h.client.post(f"/runs/{run_id}/mcp", json=batch)
        assert response.status_code == 400
        assert h.upstream.seen == []

    run(scenario())


def test_malformed_tool_call_is_refused_without_touching_the_frontier():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        response = await h.client.post(
            f"/runs/{run_id}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ""}},
        )
        assert response.json()["error"]["code"] == AGENTDNA_GATEWAY_ERROR
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
            f"/runs/{run_id}/mcp",
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
        assert answer["error"]["code"] == AGENTDNA_GATEWAY_ERROR
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


@pytest.mark.parametrize("tool", ["refused", "protocol_error", "input_required"])
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


def test_upstream_http_error_passes_through_and_leaves_the_frontier():
    async def scenario():
        h = Harness(Upstream())
        run_id = await h.open_run()
        response = await h.client.post(
            f"/runs/{run_id}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "http_500"},
            },
        )
        assert response.status_code == 500
        assert (await h.client.get(f"/runs/{run_id}")).json()["calls_in_flight"] == 0

    run(scenario())


def test_unreachable_upstream_is_a_jsonrpc_error_and_leaves_the_frontier():
    async def scenario():
        h = Harness(Upstream())

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        h.app.state.upstream = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        run_id = await h.open_run()

        response = await h.client.post(
            f"/runs/{run_id}/mcp",
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo"}},
        )
        assert response.status_code == 502
        assert response.json()["id"] == 9
        assert response.json()["error"]["code"] == AGENTDNA_GATEWAY_ERROR
        status = (await h.client.get(f"/runs/{run_id}")).json()
        assert status["calls_in_flight"] == 0
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


def test_config_requires_the_agent_name_and_upstream():
    with pytest.raises(ConfigError, match="AGENTDNA_AGENT_NAME"):
        AgentIdentityConfig.from_env({})
    with pytest.raises(ConfigError, match="AGENTDNA_GATEWAY_UPSTREAM_URL"):
        GatewayConfig.from_env({})


@pytest.mark.parametrize(
    "env",
    [
        {"AGENTDNA_GATEWAY_UPSTREAM_URL": "ftp://x/mcp"},
        {"AGENTDNA_GATEWAY_UPSTREAM_URL": "http://x/mcp", "AGENTDNA_GATEWAY_PORT": "abc"},
        {"AGENTDNA_GATEWAY_UPSTREAM_URL": "http://x/mcp", "AGENTDNA_GATEWAY_PORT": "70000"},
        {"AGENTDNA_GATEWAY_UPSTREAM_URL": "http://x/mcp", "AGENTDNA_GATEWAY_RUN_TTL_SECONDS": "0"},
    ],
)
def test_invalid_gateway_config_is_refused(env):
    with pytest.raises(ConfigError):
        GatewayConfig.from_env(env)


def test_config_reads_the_environment(tmp_path):
    policy = tmp_path / "SKILLS.md"
    policy.write_text("# skills")
    identity = AgentIdentityConfig.from_env(
        {
            "AGENTDNA_AGENT_NAME": " sqlite-agent ",
            "AGENTDNA_API_KEY": "key",
            "AGENTDNA_AGENT_POLICY_FILE": str(policy),
        }
    )
    assert identity.name == "sqlite-agent"
    assert identity.policy_file == policy

    with pytest.raises(ConfigError, match="AGENTDNA_AGENT_POLICY_FILE"):
        AgentIdentityConfig.from_env(
            {"AGENTDNA_AGENT_NAME": "a", "AGENTDNA_AGENT_POLICY_FILE": str(tmp_path / "nope")}
        )

    config = GatewayConfig.from_env(
        {
            "AGENTDNA_GATEWAY_UPSTREAM_URL": "http://127.0.0.1:8013/mcp",
            "AGENTDNA_GATEWAY_PORT": "9000",
        }
    )
    assert (config.host, config.port, config.token) == ("127.0.0.1", 9000, "")
