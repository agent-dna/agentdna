import asyncio
from types import SimpleNamespace

import pytest
import requests

from agentdna.cbac.guard import cbac_context, configure
from agentdna.cbac.mcp import cbac_intercept

AGENT_ID = "did:agent"
ALLOW_HEADERS = {
    "X-CBAC-Decision": "allow",
    "X-CBAC-Intent-Score": "0.9",
    "X-CBAC-Policy-Score": "0.8",
    "X-CBAC-Hallucination-Score": "0.95",
}


@pytest.fixture(autouse=True)
def guard_url():
    configure(cbac_url="http://cbac.test", cbac_timeout=1.0)


@pytest.fixture
def posts(monkeypatch):
    recorded = []

    def fake_post(url, json=None, timeout=None):
        recorded.append((url, json))
        if url.endswith("/authorize-cbac"):
            return SimpleNamespace(headers=dict(fake_post.headers), text="reason text")
        return SimpleNamespace(headers={}, text="{}")

    fake_post.headers = ALLOW_HEADERS
    monkeypatch.setattr(requests, "post", fake_post)
    return recorded


def make_handler():
    """An async MCP handler that records the requests it actually ran."""
    calls = []

    async def handler(request):
        calls.append(request)
        return SimpleNamespace(isError=False, content=[])

    handler.calls = calls
    return handler


def run(request, handler, *, user_intent="show me the PRs", open_ctx=True):
    async def _run():
        if not open_ctx:
            return await cbac_intercept(request, handler)
        with cbac_context(agent_id=AGENT_ID, user_intent=user_intent):
            return await cbac_intercept(request, handler)

    return asyncio.run(_run())


def request(name="create_pr", **args):
    return SimpleNamespace(name=name, args=args)


def test_allow_forwards_and_reports_lhi(posts):
    handler = make_handler()
    result = run(request(repo="acme/x"), handler)

    assert handler.calls, "allowed call must reach the handler"
    assert result.isError is False, "allowed call returns the handler's result unchanged"
    lhi = [body for url, body in posts if url.endswith("/compute-lhi")]
    assert lhi == [
        {
            "agent_id": AGENT_ID,
            "callee_name": "create_pr",
            "callee_type": "mcp",
            "output_score": 1.0,
            "intent_score": 0.9,
            "policy_score": 0.8,
            "hallucination_score": 0.95,
        }
    ]


def test_deny_short_circuits_without_forwarding(posts):
    posts_headers = {**ALLOW_HEADERS, "X-CBAC-Decision": "deny"}
    requests.post.headers = posts_headers  # served by the fake_post closure

    handler = make_handler()
    result = run(request(repo="acme/x"), handler)

    assert handler.calls == [], "denied call must never reach the handler"
    assert result.content[0].text == '{"status": "denied", "error": "reason text"}'
    assert not any(url.endswith("/compute-lhi") for url, _ in posts)


def test_no_context_is_passthrough(posts):
    handler = make_handler()
    run(request(repo="acme/x"), handler, open_ctx=False)

    assert handler.calls, "without governance the call passes straight through"
    assert posts == [], "no authorize or lhi call when governance is off"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
