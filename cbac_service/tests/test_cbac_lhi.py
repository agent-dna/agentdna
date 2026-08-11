import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("sentence_transformers")

import cbac_service.cbac as cbac_mod
from agentdna.id import get_id
from cbac_service.cbac import CBAC
from cbac_service.config import LHI_LAMBDA_DOWN, LHI_LAMBDA_UP, LHI_WEIGHTS

SCORES = {
    "intent_score": 0.9,
    "policy_score": 0.8,
    "hallucination_score": 0.95,
    "output_score": 1.0,
}

# compute_lhi only hands the session to the repository functions, which the
# `rows` fixture replaces with in-memory fakes — so an opaque stub suffices.
SESSION = SimpleNamespace()


def weighted_mean(intent_score, policy_score, hallucination_score, output_score):
    values = (intent_score, policy_score, hallucination_score, output_score)
    return sum(value * weight for value, weight in zip(values, LHI_WEIGHTS, strict=True))


@pytest.fixture
def rows(monkeypatch):
    """In-memory stand-in for the lhi_records table (one entry per interaction),
    patched over the repository functions cbac.py imports."""
    records: list[SimpleNamespace] = []

    async def fake_get_latest_trust(session, agent_id, callee_name, callee_type):
        for record in reversed(records):
            if (record.agent_id, record.callee_name, record.callee_type) == (
                agent_id,
                callee_name,
                callee_type,
            ):
                return record.trust
        return None

    async def fake_agent_has_lhi_records(session, agent_id):
        return any(record.agent_id == agent_id for record in records)

    async def fake_insert_lhi_record(session, **kwargs):
        record = SimpleNamespace(
            id=len(records) + 1, created_at=datetime.now(timezone.utc), **kwargs
        )
        records.append(record)
        return record

    monkeypatch.setattr(cbac_mod, "get_latest_trust", fake_get_latest_trust)
    monkeypatch.setattr(cbac_mod, "agent_has_lhi_records", fake_agent_has_lhi_records)
    monkeypatch.setattr(cbac_mod, "insert_lhi_record", fake_insert_lhi_record)
    return records


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


def compute(cbac, callee_name="github_tool", callee_type="tool", **overrides):
    return asyncio.run(
        cbac.compute_lhi(SESSION, "did:agent", callee_name, callee_type, **{**SCORES, **overrides})
    )


def edge_history(records, callee_name, callee_type):
    return [
        r.trust for r in records if (r.callee_name, r.callee_type) == (callee_name, callee_type)
    ]


def test_first_interaction_returns_weighted_mean(rows, tmp_path):
    cbac, _ = make_cbac(tmp_path)
    assert compute(cbac) == pytest.approx(weighted_mean(**SCORES))


def test_improving_scores_raise_trust_slowly(rows, tmp_path):
    cbac, _ = make_cbac(tmp_path)
    prev = compute(cbac, **{k: 0.5 for k in SCORES})
    trust = compute(cbac)
    s = weighted_mean(**SCORES)
    assert trust == pytest.approx(LHI_LAMBDA_UP * prev + (1 - LHI_LAMBDA_UP) * s)
    assert prev < trust < s


def test_degrading_scores_drop_trust_fast(rows, tmp_path):
    cbac, _ = make_cbac(tmp_path)
    prev = compute(cbac)
    trust = compute(cbac, **{k: 0.2 for k in SCORES})
    assert trust == pytest.approx(LHI_LAMBDA_DOWN * prev + (1 - LHI_LAMBDA_DOWN) * 0.2)
    assert trust < prev


def test_zero_component_costs_exactly_its_weight(rows, tmp_path):
    """A failed call (output=0) lowers s by w_output, not to 0 — a transient
    tool failure must not annihilate an otherwise compliant interaction."""
    cbac, _ = make_cbac(tmp_path)
    trust = compute(cbac, output_score=0.0)
    assert trust == pytest.approx(weighted_mean(**SCORES) - LHI_WEIGHTS[3] * 1.0)
    assert trust > 0.0


def test_out_of_range_score_raises_and_writes_nothing(rows, tmp_path):
    cbac, calls = make_cbac(tmp_path)
    with pytest.raises(ValueError, match="intent_score"):
        compute(cbac, intent_score=1.2)
    assert rows == []
    assert calls == []


def test_trust_is_tracked_per_callee_edge(rows, tmp_path):
    cbac, _ = make_cbac(tmp_path)
    compute(cbac)
    trust_other = compute(
        cbac, callee_name="slack_agent", callee_type="agent", **{k: 0.5 for k in SCORES}
    )
    assert trust_other == pytest.approx(0.5)


def test_same_name_different_type_are_separate_edges(rows, tmp_path):
    """(callee_name, callee_type) is the edge key: the same name with a
    different type starts a fresh trust history."""
    cbac, _ = make_cbac(tmp_path)
    compute(cbac, callee_name="helper", callee_type="tool")
    trust_agent = compute(
        cbac, callee_name="helper", callee_type="agent", **{k: 0.5 for k in SCORES}
    )
    assert trust_agent == pytest.approx(0.5)  # first interaction for helper:agent
    assert len(edge_history(rows, "helper", "tool")) == 1
    assert len(edge_history(rows, "helper", "agent")) == 1


def test_history_is_appended_not_replaced(rows, tmp_path):
    """Every interaction adds a row; the edge's trust history is the ordered
    trust column, and the latest row is the current trust the next EMA reads."""
    cbac, _ = make_cbac(tmp_path)
    t1 = compute(cbac, **{k: 0.5 for k in SCORES})
    t2 = compute(cbac)
    t3 = compute(cbac, output_score=0.0)

    history = edge_history(rows, "github_tool", "tool")
    assert history == [pytest.approx(t1), pytest.approx(t2), pytest.approx(t3)]
    # Each row keeps its component scores, so the EMA chain is auditable.
    assert rows[1].intent_score == SCORES["intent_score"]
    assert rows[2].output_score == 0.0


def test_ema_continues_across_instances(rows, tmp_path):
    """Trust state lives in the DB, not the CBAC object — a fresh instance
    (new process, restarted service) continues the same EMA chain."""
    first, _ = make_cbac(tmp_path)
    prev = compute(first)

    fresh, _ = make_cbac(tmp_path)
    trust = compute(fresh)
    s = weighted_mean(**SCORES)
    assert trust == pytest.approx(LHI_LAMBDA_UP * prev + (1 - LHI_LAMBDA_UP) * s)


def test_provenance_card_created_then_appended(rows, tmp_path):
    cbac, calls = make_cbac(tmp_path)
    compute(cbac)
    compute(cbac, callee_name="slack_agent", callee_type="agent")

    expected_card = get_id("did:agent:cbac")
    assert [(op, card) for op, card, _ in calls] == [
        ("create", expected_card),
        ("append", expected_card),
    ]


def test_chain_failure_raises_but_keeps_db_record(rows, tmp_path):
    cbac, _ = make_cbac(tmp_path)

    def boom(card_id, card_info):
        raise ConnectionError("node down")

    cbac.provenance.create_new_provenance_card = boom
    with pytest.raises(RuntimeError, match="saved locally"):
        compute(cbac)

    assert len(rows) == 1
    assert rows[0].trust == pytest.approx(weighted_mean(**SCORES))


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
