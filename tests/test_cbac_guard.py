import asyncio
from types import SimpleNamespace

import pytest
import requests

from agentdna.cbac.guard import cbac_context, cbac_guard, configure

AGENT_ID = "did:agent"
SCORE_HEADERS = {
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
    """Record every guard HTTP call; serve the authorize response from headers."""
    recorded = []

    def fake_post(url, json=None, timeout=None):
        recorded.append((url, json))
        if url.endswith("/authorize-cbac"):
            return SimpleNamespace(headers=dict(fake_post.headers), text="reason text")
        return SimpleNamespace(headers={}, text="{}")

    fake_post.headers = SCORE_HEADERS
    monkeypatch.setattr(requests, "post", fake_post)
    return recorded


def lhi_calls(recorded):
    return [body for url, body in recorded if url.endswith("/compute-lhi")]


def run_guarded(fn, *args, user_intent="show me the PRs", **kwargs):
    async def _run():
        with cbac_context(agent_id=AGENT_ID, user_intent=user_intent):
            return await fn(*args, **kwargs)

    return asyncio.run(_run())


def test_successful_call_reports_output_score_one(posts):
    @cbac_guard()
    async def github_tool(owner: str):
        return {"pull_requests": []}

    assert run_guarded(github_tool, owner="acme") == {"pull_requests": []}
    assert lhi_calls(posts) == [
        {
            "agent_id": AGENT_ID,
            "callee_name": "github_tool",
            "callee_type": "tool",
            "output_score": 1.0,
            "intent_score": 0.9,
            "policy_score": 0.8,
            "hallucination_score": 0.95,
        }
    ]


def test_error_status_dict_reports_zero(posts):
    @cbac_guard()
    async def flaky_tool():
        return {"status": "error", "error": "upstream 500"}

    run_guarded(flaky_tool)
    assert lhi_calls(posts)[0]["output_score"] == 0.0


def test_raising_tool_reports_zero_and_propagates(posts):
    @cbac_guard()
    async def exploding_tool():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        run_guarded(exploding_tool)
    assert lhi_calls(posts)[0]["output_score"] == 0.0


def test_callee_type_is_recorded(posts):
    @cbac_guard(callee_type="mcp", action="remote_search")
    async def some_tool():
        return "ok"

    run_guarded(some_tool)
    call = lhi_calls(posts)[0]
    assert call["callee_type"] == "mcp"
    assert call["callee_name"] == "remote_search"


def test_denied_call_reports_no_trust(posts, monkeypatch):
    monkeypatch.setattr(requests.post, "headers", {"X-CBAC-Decision": "deny"})

    @cbac_guard()
    async def github_tool():
        raise AssertionError("must not run")

    result = run_guarded(github_tool)
    assert result["status"] == "denied"
    assert lhi_calls(posts) == []


def test_missing_score_header_skips_report(posts, monkeypatch):
    headers = dict(SCORE_HEADERS)
    del headers["X-CBAC-Policy-Score"]  # Tier 3 decision: no numeric policy signal
    monkeypatch.setattr(requests.post, "headers", headers)

    @cbac_guard()
    async def github_tool():
        return "ok"

    assert run_guarded(github_tool) == "ok"
    assert lhi_calls(posts) == []


def test_lhi_failure_does_not_break_the_result(posts, monkeypatch):
    original = requests.post

    def failing_post(url, json=None, timeout=None):
        if url.endswith("/compute-lhi"):
            posts.append((url, json))
            raise requests.ConnectionError("cbac service down")
        return original(url, json=json, timeout=timeout)

    monkeypatch.setattr(requests, "post", failing_post)

    @cbac_guard()
    async def github_tool():
        return {"pull_requests": []}

    assert run_guarded(github_tool) == {"pull_requests": []}
    assert len(lhi_calls(posts)) == 1


def test_no_context_runs_unguarded_without_reporting(posts):
    @cbac_guard()
    async def github_tool():
        return "ok"

    assert asyncio.run(github_tool()) == "ok"
    assert posts == []
