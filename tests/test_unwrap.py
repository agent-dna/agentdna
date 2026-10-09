"""IntentWorkflow.unwrap() must return every envelope in the DAG exactly once.

verify_heavy() verifies what unwrap() returns, so an envelope missing here is
an envelope that is never verified.
"""

import json

from agentdna.helpers import _hash_content
from agentdna.types import Envelope, IntentWorkflow, load_workflow


def envelope(sender: str, payload: str, parents=None) -> Envelope:
    env = Envelope(from_=sender, payload=payload, epoch=100)
    for parent in parents or []:
        env.add_envelope(parent)
    env.hash = _hash_content(env)
    parent_hashes = ",".join(sorted(p.hash for p in parents or []))
    env.signature = f"{sender}:{env.hash}:{parent_hashes}"
    return env


def workflow(tip: Envelope) -> IntentWorkflow:
    return IntentWorkflow(id="intent", type="intent_workflow", version="2.0", envelope=tip)


def test_identical_answers_to_concurrent_calls_are_both_kept():
    root = envelope("user", "intent")
    call_a = envelope("agent", "call A", [root])
    call_b = envelope("agent", "call B", [root])
    # Same tool, same status, same second: same content hash.
    answer_a = envelope("server", "success", [call_a])
    answer_b = envelope("server", "success", [call_b])
    assert answer_a.hash == answer_b.hash

    tip = envelope("agent", "reply", [answer_a, answer_b])
    payloads = sorted(e.payload for e in workflow(tip).unwrap())

    assert payloads == ["call A", "call B", "intent", "reply", "success", "success"]


def test_a_shared_ancestor_is_listed_once():
    root = envelope("user", "intent")
    call_a = envelope("agent", "call A", [root])
    call_b = envelope("agent", "call B", [root])
    tip = envelope("agent", "reply", [call_a, call_b])

    assert [e.payload for e in workflow(tip).unwrap()].count("intent") == 1


def test_a_shared_ancestor_is_listed_once_after_a_round_trip():
    """Deserialised, the shared ancestor is two separate objects."""
    root = envelope("user", "intent")
    tip = envelope(
        "agent",
        "reply",
        [envelope("agent", "call A", [root]), envelope("agent", "call B", [root])],
    )
    reloaded = load_workflow(json.loads(workflow(tip).serialize()))

    assert [e.payload for e in reloaded.unwrap()].count("intent") == 1
