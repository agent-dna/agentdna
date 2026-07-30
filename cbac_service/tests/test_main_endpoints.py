import asyncio
import json
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
    return cbac


def authorizing(result):
    async def verify_cbac(agent_id, intended_action, user_intent):
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
    async def boom(agent_id, intended_action, user_intent):
        raise RuntimeError("policy lookup failed")

    install_cbac(monkeypatch, verify_cbac=boom)
    response = asyncio.run(main.authorize_cbac(stub_request(AUTHORIZE_BODY)))

    assert response.headers["X-CBAC-Decision"] == "error"
    assert not any(h.startswith("x-cbac-") and h != "x-cbac-decision" for h in response.headers)


def test_compute_lhi_forwards_all_scores(monkeypatch):
    calls = []

    def compute_lhi(**kwargs):
        calls.append(kwargs)
        return 0.87

    install_cbac(monkeypatch, compute_lhi=compute_lhi)
    response = asyncio.run(main.compute_lhi(stub_request(LHI_BODY)))

    assert calls == [LHI_BODY]
    assert json.loads(response.body) == {"trust": 0.87}
    assert response.status_code == 200


def test_compute_lhi_error_returns_500(monkeypatch):
    def boom(**kwargs):
        raise ValueError("intent_score must be in [0, 1], got 1.2")

    install_cbac(monkeypatch, compute_lhi=boom)
    response = asyncio.run(main.compute_lhi(stub_request(dict(LHI_BODY, intent_score=1.2))))

    assert response.status_code == 500
    assert "must be in [0, 1]" in json.loads(response.body)["error"]
