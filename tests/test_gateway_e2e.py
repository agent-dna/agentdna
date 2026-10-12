"""The gateway end to end, with nothing faked but the admin whitelist.

    user ─▶ agent ── MCPGateway(...).session(AGENT, ...) ──▶ gateway
                └── plain fastmcp.Client ──▶ gateway ──▶ FastMCP + AgentDNAMCPMiddleware

Real signatures against the provenance node, real MCP servers running the
AgentDNA middleware, and an MCP client with no AgentDNA adapter installed. The
agent ends with a reply whose chain holds every tool call, and which verifies
in heavy mode.
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
from agentdna.mcp.gateway import MCPGateway, WorkflowRejected
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


def _tool_actor() -> AgentDNA:
    return AgentDNA(
        name=f"tool-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_TOOL,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        config_dir=CONFIG_DIR,
    )


def _tool_calls(reply, agent) -> list[tuple[str, str]]:
    return sorted(
        (
            json.loads(e.payload)["params"]["name"],
            json.dumps(json.loads(e.payload)["params"]["arguments"]),
        )
        for e in reply.unwrap()
        if e.from_ == agent.get_actor_id() and e.payload.startswith('{"method":"tools/call"')
    )


@pytest.fixture
def servers(monkeypatch):
    """Two MCP servers, each with its own AgentDNA identity."""

    # The one thing not exercised: test agents are not on the admin whitelist.
    monkeypatch.setattr(fastmcp_middleware, "agent_whitelist_check", lambda *args, **kwargs: None)

    maths_dna, words_dna = _tool_actor(), _tool_actor()

    maths = FastMCP("maths")
    maths.add_middleware(AgentDNAMCPMiddleware(maths_dna))

    @maths.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @maths.tool
    def explode() -> str:
        """Always fails."""
        raise ValueError("boom")

    words = FastMCP("words")
    words.add_middleware(AgentDNAMCPMiddleware(words_dna))

    @words.tool
    def shout(text: str) -> str:
        """Upper-case some text."""
        return text.upper()

    maths_url, maths_server = _serve(maths.http_app(path="/mcp"))
    words_url, words_server = _serve(words.http_app(path="/mcp"))

    yield {"maths": f"{maths_url}/mcp", "words": f"{words_url}/mcp"}, maths_dna, words_dna

    maths_server.should_exit = True
    words_server.should_exit = True


def test_in_process_gateway_with_a_plain_mcp_client(servers, user, agent):
    urls, maths_dna, words_dna = servers
    mcp = MCPGateway(urls)

    incoming = user.build(payload="add some numbers and shout")

    async def agent_run():
        async with mcp.session(agent, incoming) as session:
            async with Client(session.url("maths")) as maths, Client(session.url("words")) as words:
                first = await maths.call_tool("add", {"a": 1, "b": 2})
                second, third = await asyncio.gather(
                    maths.call_tool("add", {"a": 2, "b": 3}),
                    maths.call_tool("add", {"a": 4, "b": 5}),
                )
                shouted = await words.call_tool("shout", {"text": "done"})
                failed = await maths.call_tool("explode", raise_on_error=False)
        return [first, second, third, shouted, failed], session

    try:
        results, session = asyncio.run(agent_run())
    finally:
        mcp.close()

    assert [r.data for r in results[:4]] == [3, 5, 9, "DONE"]
    assert results[4].is_error

    # The run ended on the tool failure, signed by the maths server.
    [tip] = session.workflows
    assert tip.get_latest_envelope_actor() == maths_dna.get_actor_id()
    assert tip.get_latest_envelope().status_code == TOOL_EXECUTION_FAILED

    reply = agent.build(payload="3, 5, 9 and DONE", previous_workflows=session.workflows)
    assert reply.get_latest_envelope_actor() == agent.get_actor_id()
    assert agent.verify(reply, mode=VERIFY_HEAVY) == RESULT_OK
    assert reply.id == incoming.id
    assert reply.get_root_envelope_actor() == user.get_actor_id()

    assert _tool_calls(reply, agent) == sorted(
        [
            ("add", '{"a": 1, "b": 2}'),
            ("add", '{"a": 2, "b": 3}'),
            ("add", '{"a": 4, "b": 5}'),
            ("explode", "{}"),
            ("shout", '{"text": "done"}'),
        ]
    )
    servers_in_chain = [e.from_ for e in reply.unwrap()]
    assert servers_in_chain.count(maths_dna.get_actor_id()) == 4
    assert servers_in_chain.count(words_dna.get_actor_id()) == 1


def test_in_process_gateway_from_synchronous_code(servers, user, agent):
    urls, *_ = servers
    mcp = MCPGateway(urls)
    incoming = user.build(payload="nothing to do")

    try:
        with mcp.session(agent, incoming) as session:
            assert session.servers == ["maths", "words"]
            assert [w.get_latest_envelope().hash for w in session.frontier()] == [
                incoming.envelope.hash
            ]
    finally:
        mcp.close()

    # No tool calls: the final workflow is the incoming one, unchanged.
    assert [w.get_latest_envelope().hash for w in session.workflows] == [incoming.envelope.hash]


def test_in_process_gateway_refuses_a_tampered_workflow(servers, user, agent):
    urls, *_ = servers
    mcp = MCPGateway(urls)
    incoming = user.build(payload="MFA is mandatory")
    incoming.envelope.payload = "MFA is optional"

    try:
        with pytest.raises(WorkflowRejected), mcp.session(agent, incoming):
            pass
    finally:
        mcp.close()


def test_in_process_gateway_has_no_run_endpoints(servers, user, agent):
    """Runs are opened in memory, so nothing else on the machine can open one
    and have tool calls signed as this agent. Only the session's MCP URL works."""

    urls, *_ = servers
    mcp = MCPGateway(urls)

    try:
        with mcp.session(agent, user.build(payload="intent")) as session:
            assert mcp._embedded is not None
            gateway_url = mcp._embedded.url
            workflow = user.build(payload="someone else's intent")

            response = httpx.post(
                f"{gateway_url}/runs", json={"workflows": [json.loads(workflow.serialize())]}
            )
            assert response.status_code == 404
            assert httpx.get(f"{gateway_url}/runs/{session.run_id}").status_code == 404
            assert httpx.delete(f"{gateway_url}/runs/{session.run_id}").status_code == 404

            # The session itself is unaffected.
            assert len(session.frontier()) == 1
    finally:
        mcp.close()


def test_unknown_run_never_reaches_the_server(servers, user, agent):
    urls, *_ = servers
    mcp = MCPGateway(urls)
    try:
        with mcp.session(agent, user.build(payload="intent")):
            assert mcp._embedded is not None
            response = httpx.post(
                f"{mcp._embedded.url}/runs/not-a-run/mcp/maths",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add"}},
            )
            assert response.status_code == 404
    finally:
        mcp.close()
