"""The gateway end to end, with nothing faked but the admin whitelist.

    user ─▶ agent app ─(GatewayClient)─▶ gateway ─▶ FastMCP + AgentDNAMCPMiddleware
                 └── plain fastmcp.Client ──┘

Real signatures against the provenance node, a real MCP server running the
AgentDNA middleware, and an MCP client with no AgentDNA adapter installed.
The agent ends with a reply whose chain holds every tool call, and which
verifies in heavy mode.
"""

import asyncio
import json
import secrets
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP

import agentdna.mcp.server.fastmcp as fastmcp_middleware
from agentdna.core import AgentDNA
from agentdna.error import RESULT_OK, TOOL_EXECUTION_FAILED
from agentdna.mcp.gateway import GatewayClient
from agentdna.mcp.gateway.app import create_app
from agentdna.mcp.gateway.config import GatewayConfig
from agentdna.mcp.server.fastmcp import AgentDNAMCPMiddleware
from agentdna.types import ACTOR_TYPE_TOOL, VERIFY_HEAVY
from tests.conftest import CONFIG_DIR, DUMMY_API_KEY, ONLINE_PROVENANCE_NODE


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(app) -> tuple[str, uvicorn.Server]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


@pytest.fixture
def stack(agent, monkeypatch):
    # The one thing not exercised: test agents are not on the admin whitelist.
    monkeypatch.setattr(fastmcp_middleware, "agent_whitelist_check", lambda *args, **kwargs: None)

    tool_dna = AgentDNA(
        name=f"tool-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_TOOL,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        config_dir=CONFIG_DIR,
    )

    mcp = FastMCP("gateway-e2e")
    mcp.add_middleware(AgentDNAMCPMiddleware(tool_dna))

    @mcp.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @mcp.tool
    def explode() -> str:
        """Always fails."""
        raise ValueError("boom")

    upstream_url, upstream = _serve(mcp.http_app(path="/mcp"))
    gateway_url, gateway = _serve(
        create_app(agent, GatewayConfig(upstream_url=f"{upstream_url}/mcp"))
    )

    yield gateway_url, tool_dna

    gateway.should_exit = True
    upstream.should_exit = True


def test_agent_app_uses_the_gateway_with_a_plain_mcp_client(stack, user, agent):
    gateway_url, tool_dna = stack
    gateway = GatewayClient(gateway_url)

    incoming = user.build(payload="add some numbers")
    assert agent.verify(incoming) == RESULT_OK

    async def agent_run():
        async with gateway.run(incoming) as run:
            async with Client(run.mcp_url) as client:
                first = await client.call_tool("add", {"a": 1, "b": 2})
                second, third = await asyncio.gather(
                    client.call_tool("add", {"a": 2, "b": 3}),
                    client.call_tool("add", {"a": 4, "b": 5}),
                )
                failed = await client.call_tool("explode", raise_on_error=False)
            workflows = await run.aclose()
        return [first, second, third, failed], workflows

    results, workflows = asyncio.run(agent_run())

    assert [r.data for r in results[:3]] == [3, 5, 9]
    assert results[3].is_error

    # The run ended on the tool failure, signed by the server.
    [tip] = workflows
    assert tip.get_latest_envelope_actor() == tool_dna.get_actor_id()
    assert tip.get_latest_envelope().status_code == TOOL_EXECUTION_FAILED

    reply = agent.build(payload="the sums are 3, 5 and 9", previous_workflows=workflows)
    assert agent.verify(reply, mode=VERIFY_HEAVY) == RESULT_OK
    assert reply.id == incoming.id
    assert reply.get_root_envelope_actor() == user.get_actor_id()

    envelopes = reply.unwrap()
    requests = [
        json.loads(e.payload)["params"]
        for e in envelopes
        if e.from_ == agent.get_actor_id() and e.payload.startswith('{"method":"tools/call"')
    ]
    assert sorted((r["name"], json.dumps(r["arguments"])) for r in requests) == sorted(
        [
            ("add", '{"a": 1, "b": 2}'),
            ("add", '{"a": 2, "b": 3}'),
            ("add", '{"a": 4, "b": 5}'),
            ("explode", "{}"),
        ]
    )
    server_results = [e for e in envelopes if e.from_ == tool_dna.get_actor_id()]
    assert len(server_results) == 4


def test_unknown_run_never_reaches_the_server(stack):
    gateway_url, _ = stack
    response = httpx.post(
        f"{gateway_url}/runs/not-a-run/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add"}},
    )
    assert response.status_code == 404


def test_sync_run_round_trip(stack, user):
    gateway_url, _ = stack
    incoming = user.build(payload="nothing to do")

    with GatewayClient(gateway_url).run(incoming) as run:
        assert run.mcp_url == f"{gateway_url}/runs/{run.run_id}/mcp"
        assert [w.get_latest_envelope().hash for w in run.frontier()] == [incoming.envelope.hash]

    assert run.closed
    assert [w.get_latest_envelope().hash for w in run.workflows] == [incoming.envelope.hash]
