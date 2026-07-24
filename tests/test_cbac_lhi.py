import json
from types import SimpleNamespace

import pytest

pytest.importorskip("sentence_transformers")

from agentdna.cbac import _LHI_LAMBDA_DOWN, _LHI_LAMBDA_UP, _LHI_WEIGHTS, CBAC
from agentdna.id import get_id

SCORES = {
    "intent_score": 0.9,
    "policy_score": 0.8,
    "hallucination_score": 0.95,
    "output_score": 1.0,
}


def geometric_mean(intent_score, policy_score, hallucination_score, output_score):
    values = (intent_score, policy_score, hallucination_score, output_score)
    s = 1.0
    for value, weight in zip(values, _LHI_WEIGHTS):
        s *= value**weight
    return s


def make_cbac(tmp_path):
    calls = []
    provenance = SimpleNamespace(
        config_dir=str(tmp_path),
        create_new_provenance_card=lambda card_id, card_info: calls.append(
            ("create", card_id, card_info)
        ),
        append_to_provenance_card=lambda card_id, card_info: calls.append(
            ("append", card_id, card_info)
        ),
    )
    return CBAC(provenance=provenance), calls  # type: ignore[arg-type]


def test_first_interaction_returns_geometric_mean(tmp_path):
    cbac, _ = make_cbac(tmp_path)
    trust = cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)
    assert trust == pytest.approx(geometric_mean(**SCORES))


def test_improving_scores_raise_trust_slowly(tmp_path):
    cbac, _ = make_cbac(tmp_path)
    low = {k: 0.5 for k in SCORES}
    prev = cbac.compute_lhi("did:agent", "github_tool", "tool", **low)
    trust = cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)
    s = geometric_mean(**SCORES)
    assert trust == pytest.approx(_LHI_LAMBDA_UP * prev + (1 - _LHI_LAMBDA_UP) * s)
    assert prev < trust < s


def test_degrading_scores_drop_trust_fast(tmp_path):
    cbac, _ = make_cbac(tmp_path)
    prev = cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)
    low = {k: 0.2 for k in SCORES}
    trust = cbac.compute_lhi("did:agent", "github_tool", "tool", **low)
    assert trust == pytest.approx(_LHI_LAMBDA_DOWN * prev + (1 - _LHI_LAMBDA_DOWN) * 0.2)
    assert trust < prev


def test_zero_component_zeroes_instantaneous_score(tmp_path):
    cbac, _ = make_cbac(tmp_path)
    scores = dict(SCORES, policy_score=0.0)
    assert cbac.compute_lhi("did:agent", "github_tool", "tool", **scores) == 0.0


def test_out_of_range_score_raises(tmp_path):
    cbac, calls = make_cbac(tmp_path)
    with pytest.raises(ValueError, match="intent_score"):
        cbac.compute_lhi("did:agent", "github_tool", "tool", **dict(SCORES, intent_score=1.2))
    assert calls == []


def test_trust_is_tracked_per_callee_edge(tmp_path):
    cbac, _ = make_cbac(tmp_path)
    cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)
    low = {k: 0.5 for k in SCORES}
    trust_other = cbac.compute_lhi("did:agent", "slack_agent", "agent", **low)
    assert trust_other == pytest.approx(0.5)


def test_store_round_trips_across_instances(tmp_path):
    cbac, _ = make_cbac(tmp_path)
    prev = cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)

    fresh, _ = make_cbac(tmp_path)
    trust = fresh.compute_lhi("did:agent", "github_tool", "tool", **SCORES)
    s = geometric_mean(**SCORES)
    assert trust == pytest.approx(_LHI_LAMBDA_UP * prev + (1 - _LHI_LAMBDA_UP) * s)

    store = json.loads((tmp_path / "trust_scores.json").read_text())
    entry = store["did:agent"]["callees"]["github_tool"]
    assert entry["type"] == "tool"
    assert entry["trust"] == pytest.approx(trust)
    assert set(entry["scores"]) == {"intent", "policy", "hallucination", "output"}


def test_provenance_card_created_then_appended(tmp_path):
    cbac, calls = make_cbac(tmp_path)
    cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)
    cbac.compute_lhi("did:agent", "slack_agent", "agent", **SCORES)

    expected_card = get_id("did:agent:lhi")
    assert [(op, card) for op, card, _ in calls] == [
        ("create", expected_card),
        ("append", expected_card),
    ]
    record = json.loads(calls[1][2])
    assert record["type"] == "lhi_record"
    assert record["callee"] == {"name": "slack_agent", "type": "agent"}
    assert record["trust"] == pytest.approx(geometric_mean(**SCORES))


def test_chain_failure_raises_but_keeps_local_update(tmp_path):
    cbac, _ = make_cbac(tmp_path)

    def boom(card_id, card_info):
        raise ConnectionError("node down")

    cbac.provenance.create_new_provenance_card = boom
    with pytest.raises(RuntimeError, match="saved locally"):
        cbac.compute_lhi("did:agent", "github_tool", "tool", **SCORES)

    store = json.loads((tmp_path / "trust_scores.json").read_text())
    assert store["did:agent"]["callees"]["github_tool"]["trust"] == pytest.approx(
        geometric_mean(**SCORES)
    )


def test_hallucination_score_orientation(tmp_path):
    pytest.importorskip("transformers")
    cbac, _ = make_cbac(tmp_path)
    try:
        contradicted = cbac.hallucination_score(
            "The capital of France is Berlin.", "The capital of France is Paris."
        )
        grounded = cbac.hallucination_score("I am in California", "I am in United States.")
    except Exception as exc:
        pytest.skip(f"HHEM model unavailable: {exc}")
    assert 0.0 <= contradicted <= 1.0
    assert 0.0 <= grounded <= 1.0
    assert contradicted < 0.5 < grounded
