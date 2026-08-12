import asyncio
import base64
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("sentence_transformers")

import cbac_service.cbac as cbac_mod
from cbac_service.cbac import CBAC

AGENT_ID = "did:agent"


def _make_provenance(tmp_path, policy_text="Agents may read pull requests."):
    policy_b64 = base64.b64encode(policy_text.encode()).decode()
    return SimpleNamespace(
        config_dir=str(tmp_path),
        get_latest_provenance_record=lambda actor_id: {
            "type": "agent",
            "id": actor_id,
            "metadata": {},
            "policy": policy_b64,
        },
        create_new_provenance_card=lambda card_id, card_info: None,
        append_to_provenance_card=lambda card_id, card_info: None,
    )


def make_verify_cbac(tmp_path, monkeypatch, policy_text="Agents may read pull requests."):
    """CBAC instance with the policy pipeline stubbed out (no NLI/encoder model
    load), so it deterministically falls through Tier 1/2/3 to Tier 3's
    no-backend "advise" — leaving `hallucination_score` as the only real model
    call, which is the thing under test here.
    """
    cbac = CBAC(provenance=_make_provenance(tmp_path, policy_text))
    monkeypatch.setattr(cbac, "_classify_chunks", lambda chunks: (chunks, []))
    monkeypatch.setattr(
        cbac,
        "_get_encoder",
        lambda: SimpleNamespace(
            encode=lambda texts, normalize_embeddings=True: np.zeros((len(texts), 4))
        ),
    )
    monkeypatch.setattr(
        cbac, "_nli_scores", lambda premise, hypothesis: {"contradiction": 0.0, "entailment": 0.0}
    )

    # DB search layer stubbed to in-memory so the pipeline runs without Postgres:
    # cache always fresh, one dummy chunk, and equal allowed/forbidden scores
    # (gap 0) so Tier 1 escalates to Tier 2, which — NLI stubbed — reaches Tier 3.
    async def _hash_matches(session, agent_id, policy_hash):
        return True

    async def _get_chunks(session, agent_id):
        return ["allowed chunk"]

    async def _search(session, agent_id, intent_vec, *args, chunk_type="allowed", **kwargs):
        return [SimpleNamespace(score=0.5, chunk_text="allowed chunk")]

    monkeypatch.setattr(cbac_mod, "policy_hash_matches", _hash_matches)
    monkeypatch.setattr(cbac_mod, "get_policy_chunks", _get_chunks)
    monkeypatch.setattr(cbac_mod, "vector_search", _search)
    monkeypatch.setattr(cbac_mod, "hybrid_search", _search)
    return cbac


def test_hallucination_score_attached_when_reached(tmp_path, monkeypatch):
    cbac = make_verify_cbac(tmp_path, monkeypatch)
    result = asyncio.run(
        cbac.verify_cbac(
            session=None,
            agent_id=AGENT_ID,
            intended_action="read pull requests",
            user_intent="Please show me the pull requests",
        )
    )
    assert result.decision == "advise"
    assert result.hallucination_score is not None
    assert 0.0 <= result.hallucination_score <= 1.0
    # Check-1 always runs when user_intent is supplied, so intent_score is set...
    assert result.intent_score is not None
    assert 0.0 <= result.intent_score <= 1.0
    # ...but this pipeline is stubbed to fall through to Tier 3, which has no
    # numeric signal, so policy_score stays None.
    assert result.policy_score is None


def test_hallucination_score_none_without_user_intent(tmp_path, monkeypatch):
    cbac = make_verify_cbac(tmp_path, monkeypatch)
    result = asyncio.run(
        cbac.verify_cbac(
            session=None, agent_id=AGENT_ID, intended_action="read pull requests", user_intent=None
        )
    )
    assert result.decision == "advise"
    assert result.hallucination_score is None
    assert result.intent_score is None


def test_hallucination_scoring_failure_does_not_change_decision(tmp_path, monkeypatch):
    cbac = make_verify_cbac(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("HHEM unavailable")

    monkeypatch.setattr(cbac, "hallucination_score", boom)
    result = asyncio.run(
        cbac.verify_cbac(
            session=None,
            agent_id=AGENT_ID,
            intended_action="read pull requests",
            user_intent="Please show me the pull requests",
        )
    )
    assert result.decision == "advise"
    assert result.hallucination_score is None


def test_intent_score_is_one_minus_contradiction(tmp_path, monkeypatch):
    cbac = make_verify_cbac(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cbac, "_nli_scores", lambda premise, hypothesis: {"contradiction": 0.3, "entailment": 0.1}
    )
    drift, intent_score = asyncio.run(cbac._check1_drift("user intent", "agent action"))
    assert drift is None  # 0.3 < contradiction_threshold, no deny
    assert intent_score == pytest.approx(0.7)


def test_policy_score_normalized_on_tier1_decision(tmp_path, monkeypatch):
    cbac = make_verify_cbac(tmp_path, monkeypatch)

    # Tier-1 search: allowed cosine 1.0, forbidden 0.0 -> gap 1.0, clamps above allow_gap.
    async def _vsearch(session, agent_id, intent_vec, *args, chunk_type="allowed", **kwargs):
        score = 1.0 if chunk_type == "allowed" else 0.0
        return [SimpleNamespace(score=score, chunk_text=f"{chunk_type} chunk")]

    monkeypatch.setattr(cbac_mod, "vector_search", _vsearch)

    decision, _reason, policy_score = asyncio.run(
        cbac._tiered_decision(None, AGENT_ID, "intent text")
    )
    assert decision == "allow"
    assert policy_score == pytest.approx(1.0)


def test_hallucination_score_not_computed_on_early_hard_fail(tmp_path, monkeypatch):
    cbac = make_verify_cbac(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cbac,
        "hallucination_score",
        lambda *a, **k: pytest.fail("hallucination_score must not run on an early hard-fail"),
    )
    result = asyncio.run(
        cbac.verify_cbac(
            session=None,
            agent_id=AGENT_ID,
            intended_action="",
            user_intent="Please show me the pull requests",
        )
    )
    assert result.decision == "deny"
    assert result.hallucination_score is None
