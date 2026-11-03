"""AgentDNA.create_sub_agent, with a stand-in provenance client: no network."""

import json
import re

import pytest

import agentdna.core as core
from agentdna.card import build_swarm_delegate_payload
from agentdna.config import get_actor_registry_entry
from agentdna.core import AgentDNA
from agentdna.id import get_agent_card_id, get_id
from agentdna.types import ACTOR_TYPE_AGENT, ACTOR_TYPE_TOOL

POLICY_FILE = "tests/data/dummy_skills.md"


def actor_id(name: str) -> str:
    """A stand-in actor ID for `name`: an IPFS CID v0, like a real one."""
    return get_id(name)


class FakeProvenance:
    """Stands in for Provenance: an actor ID per name, and the child cards created."""

    child_cards: list[dict] = []
    fail_with: Exception | None = None

    def __init__(self, name, provenance_url="", api_key="", config_path="", **kwargs):
        self.provenance_id = actor_id(name)
        self.provenance_url = provenance_url
        self.api_key = api_key

    def create_new_child_provenance_card(self, parent_card_id, card_info, child_nft_id):
        if FakeProvenance.fail_with is not None:
            raise FakeProvenance.fail_with
        FakeProvenance.child_cards.append(
            {
                "by": self.provenance_id,
                "parent_card_id": parent_card_id,
                "card_info": json.loads(card_info),
                "child_nft_id": child_nft_id,
            }
        )
        return child_nft_id, "tx-1"


@pytest.fixture
def parent(monkeypatch, tmp_path):
    FakeProvenance.child_cards = []
    FakeProvenance.fail_with = None
    monkeypatch.setattr(core, "Provenance", FakeProvenance)

    def no_registration(*args, **kwargs):
        raise AssertionError("a sub-agent must not be registered for whitelisting")

    monkeypatch.setattr(core.requests, "post", no_registration)

    return AgentDNA(
        name="orchestrator",
        type=ACTOR_TYPE_AGENT,
        provenance_layer_url="https://provenance.example",
        admin_server_url="https://admin.example",
        api_key="key-1",
        config_dir=str(tmp_path),
        metadata={"team": "research"},
        agent_policy_file=POLICY_FILE,
        skip_actor_id_registration=True,
        log_format="json",
    )


def registry_entry(parent, sub_agent_id):
    entry = get_actor_registry_entry(parent.config_dir, sub_agent_id)
    assert entry is not None
    return entry


def test_a_sub_agent_without_a_name_gets_a_random_suffix(parent):
    sub_agent_id = parent.create_sub_agent()

    name = registry_entry(parent, sub_agent_id).actor_name
    assert re.fullmatch(r"orchestrator-[A-Za-z0-9]{7}", name)
    assert sub_agent_id == actor_id(name)
    assert re.fullmatch(r"Qm[1-9A-HJ-NP-Za-km-z]{44}", sub_agent_id)  # CID v0


def test_a_given_name_is_appended_to_the_parents(parent):
    sub_agent_id = parent.create_sub_agent(name="researcher-1")

    assert sub_agent_id == actor_id("orchestrator-researcher-1")
    assert registry_entry(parent, sub_agent_id).actor_name == "orchestrator-researcher-1"


def test_each_sub_agent_is_its_own_identity(parent):
    first, second = parent.create_sub_agent(), parent.create_sub_agent()
    assert len({first, second, parent.get_actor_id()}) == 3


def test_no_agentdna_instance_is_created_for_the_sub_agent(parent, monkeypatch):
    def no_init(*args, **kwargs):
        raise AssertionError("create_sub_agent must not create an AgentDNA instance")

    monkeypatch.setattr(core.AgentDNA, "__init__", no_init)
    assert parent.create_sub_agent(name="worker") == actor_id("orchestrator-worker")


def test_the_parent_records_the_delegation_as_a_child_of_its_card(parent):
    sub_agent_id = parent.create_sub_agent(name="worker")

    assert FakeProvenance.child_cards == [
        {
            "by": actor_id("orchestrator"),
            "parent_card_id": get_agent_card_id(actor_id("orchestrator")),
            "card_info": {
                "type": "swarm_delegate",
                "parentAgentId": actor_id("orchestrator"),
                "subAgentId": actor_id("orchestrator-worker"),
                "subAgentName": "orchestrator-worker",
            },
            "child_nft_id": get_agent_card_id(sub_agent_id),
        }
    ]


def test_the_sub_agent_is_kept_in_the_actor_registry(parent):
    sub_agent_id = parent.create_sub_agent(name="worker")

    entry = registry_entry(parent, sub_agent_id)
    assert (entry.actor_name, entry.actor_card_id) == (
        "orchestrator-worker",
        get_agent_card_id(sub_agent_id),
    )


def test_a_sub_agent_can_create_sub_agents_of_its_own(parent):
    lead_id = parent.create_sub_agent(name="lead")
    lead = AgentDNA(
        name=registry_entry(parent, lead_id).actor_name,
        type=ACTOR_TYPE_AGENT,
        config_dir=parent.config_dir,
        skip_actor_id_registration=True,
    )
    assert lead.get_actor_id() == lead_id
    assert lead.card_id == get_agent_card_id(lead_id)  # found in the registry

    helper_id = lead.create_sub_agent(name="helper")

    assert helper_id == actor_id("orchestrator-lead-helper")
    assert FakeProvenance.child_cards[-1]["by"] == lead_id
    assert FakeProvenance.child_cards[-1]["parent_card_id"] == get_agent_card_id(lead_id)


def test_only_agents_can_create_sub_agents(parent, tmp_path):
    tool = AgentDNA(
        name="search-tool",
        type=ACTOR_TYPE_TOOL,
        config_dir=str(tmp_path),
        skip_actor_id_registration=True,
    )

    with pytest.raises(ValueError, match="only actors of type 'agent'"):
        tool.create_sub_agent()
    assert FakeProvenance.child_cards == []


def test_a_card_that_cannot_be_created_is_an_error(parent):
    FakeProvenance.fail_with = Exception("parent NFT not found")

    with pytest.raises(RuntimeError, match="failed to create sub-agent card.*parent NFT not found"):
        parent.create_sub_agent(name="worker")


def test_the_swarm_delegate_payload():
    parent_id, sub_id = actor_id("parent"), actor_id("parent-sub")
    assert build_swarm_delegate_payload(parent_id, sub_id, "parent-sub") == {
        "type": "swarm_delegate",
        "parentAgentId": parent_id,
        "subAgentId": sub_id,
        "subAgentName": "parent-sub",
    }
