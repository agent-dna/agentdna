import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("sentence_transformers")

from cbac_service import main
from cbac_service.cbac import CBACResult


def stub_request(body):
    async def _json():
        return body

    return SimpleNamespace(json=_json)


def install_cbac(monkeypatch, **attrs):
    cbac = SimpleNamespace(**attrs)
    monkeypatch.setattr(main, "_get_cbac", lambda: cbac)

    # The authorize/precompute endpoints open get_session(); yield a dummy
    # session so they run without a real DB (the stubbed cbac ignores it).
    @asynccontextmanager
    async def _fake_get_session():
        yield SimpleNamespace()

    monkeypatch.setattr(main, "get_session", _fake_get_session)
    return cbac


def authorizing(result):
    async def verify_cbac(session, agent_id, intended_action, user_intent):
        return result

    return verify_cbac


AUTHORIZE_BODY = {
    "agent_id": "did:agent",
    "intended_action": "read pull requests",
    "user_intent": "show me the PRs",
}

LHI_BODY = {
    "agent_id": "did:agent",
    "callee_name": "github_tool",
    "callee_type": "tool",
    "intent_score": 0.9,
    "policy_score": 0.8,
    "hallucination_score": 0.95,
    "output_score": 1.0,
}


def test_authorize_returns_score_headers(monkeypatch):
    install_cbac(
        monkeypatch,
        verify_cbac=authorizing(
            CBACResult(
                decision="allow",
                reason="Tier 1 allow",
                intent_score=0.9,
                policy_score=0.8,
                hallucination_score=0.95,
            )
        ),
    )
    response = asyncio.run(main.authorize_cbac(stub_request(AUTHORIZE_BODY)))

    assert response.headers["X-CBAC-Decision"] == "allow"
    assert float(response.headers["X-CBAC-Intent-Score"]) == 0.9
    assert float(response.headers["X-CBAC-Policy-Score"]) == 0.8
    assert float(response.headers["X-CBAC-Hallucination-Score"]) == 0.95
    assert response.body == b"Tier 1 allow"


def test_authorize_omits_headers_for_missing_scores(monkeypatch):
    install_cbac(
        monkeypatch,
        verify_cbac=authorizing(
            CBACResult(decision="advise", reason="Tier 3 inconclusive", intent_score=0.7)
        ),
    )
    response = asyncio.run(main.authorize_cbac(stub_request(AUTHORIZE_BODY)))

    assert response.headers["X-CBAC-Decision"] == "advise"
    assert "X-CBAC-Intent-Score" in response.headers
    assert "X-CBAC-Policy-Score" not in response.headers
    assert "X-CBAC-Hallucination-Score" not in response.headers


def test_authorize_failure_sends_no_score_headers(monkeypatch):
    async def boom(session, agent_id, intended_action, user_intent):
        raise RuntimeError("policy lookup failed")

    install_cbac(monkeypatch, verify_cbac=boom)
    response = asyncio.run(main.authorize_cbac(stub_request(AUTHORIZE_BODY)))

    assert response.headers["X-CBAC-Decision"] == "error"
    assert not any(h.startswith("x-cbac-") and h != "x-cbac-decision" for h in response.headers)


def test_compute_lhi_forwards_all_scores(monkeypatch):
    calls = []

    async def compute_lhi(session, **kwargs):
        calls.append(kwargs)
        return 0.87

    install_cbac(monkeypatch, compute_lhi=compute_lhi)
    response = asyncio.run(main.compute_lhi(stub_request(LHI_BODY)))

    assert calls == [LHI_BODY]
    assert json.loads(response.body) == {"trust": 0.87}
    assert response.status_code == 200


def test_compute_lhi_error_returns_500(monkeypatch):
    async def boom(session, **kwargs):
        raise ValueError("intent_score must be in [0, 1], got 1.2")

    install_cbac(monkeypatch, compute_lhi=boom)
    response = asyncio.run(main.compute_lhi(stub_request(dict(LHI_BODY, intent_score=1.2))))

    assert response.status_code == 500
    assert "must be in [0, 1]" in json.loads(response.body)["error"]


def _recording_precompute(calls, result=7):
    async def precompute_policy(session, agent_id, policy):
        calls.append({"agent_id": agent_id, "policy": policy})
        return result

    return precompute_policy


def test_precompute_forwards_supplied_policy(monkeypatch):
    calls = []
    install_cbac(monkeypatch, index_policy=_recording_precompute(calls))
    response = asyncio.run(
        main.precompute_policy(
            stub_request({"agent_id": "did:agent", "policy": "allowed-actions: read"})
        )
    )

    assert calls == [{"agent_id": "did:agent", "policy": "allowed-actions: read"}]
    assert json.loads(response.body) == {"agent_id": "did:agent", "chunks_stored": 7}


def test_precompute_without_policy_passes_none(monkeypatch):
    # None is the signal to read the agent's card from the Provenance Layer.
    calls = []
    install_cbac(monkeypatch, index_policy=_recording_precompute(calls))
    asyncio.run(main.precompute_policy(stub_request({"agent_id": "did:agent"})))

    assert calls == [{"agent_id": "did:agent", "policy": None}]


def test_precompute_rejects_non_string_policy(monkeypatch):
    async def never(session, agent_id, policy):
        raise AssertionError("should not reach the engine")

    install_cbac(monkeypatch, precompute_policy=never)
    response = asyncio.run(
        main.precompute_policy(stub_request({"agent_id": "did:agent", "policy": {"a": 1}}))
    )

    assert response.status_code == 400
    assert json.loads(response.body)["error"] == "policy must be a string"
