from pathlib import Path
import secrets

import pytest

from agentdna.core import AgentDNA
from agentdna.types import (
    ACTOR_TYPE_AGENT,
    ACTOR_TYPE_USER,
)

ONLINE_PROVENANCE_NODE = "https://chain-connector-2-dev.rubix.net"
DUMMY_API_KEY = "31c1fd3f-5f2e-4dd1-9693-529de70c8347"
TEST_POLICY_FILE = Path(__file__).parent / "data" / "dummy_skills.md"
CONFIG_DIR = str(Path(__file__).parent / "temp")


@pytest.fixture
def user():
    return AgentDNA(
        name=f"user-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_USER,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        config_dir=CONFIG_DIR,
    )


@pytest.fixture
def agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )


@pytest.fixture
def second_agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )


@pytest.fixture
def worker_1_agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )

@pytest.fixture
def worker_2_agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )

@pytest.fixture
def orchestrator_agent():
    return AgentDNA(
        name=f"agent-test-{secrets.token_hex(8)}",
        type=ACTOR_TYPE_AGENT,
        api_key=DUMMY_API_KEY,
        provenance_layer_url=ONLINE_PROVENANCE_NODE,
        skip_actor_id_registration=True,
        agent_policy_file=TEST_POLICY_FILE,
        config_dir=CONFIG_DIR,
    )


