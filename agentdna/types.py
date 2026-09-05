from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .error import RESULT_OK

ACTOR_TYPE_AGENT = "agent"
ACTOR_TYPE_USER = "user"
ACTOR_TYPE_TOOL = "tool"
supported_actors = [ACTOR_TYPE_AGENT, ACTOR_TYPE_USER, ACTOR_TYPE_TOOL]

VERIFY_LIGHT = "light"
VERIFY_HEAVY = "heavy"
VERIFY_BOUNDARY = "boundary"
supported_verification_modes = [
    VERIFY_LIGHT,
    VERIFY_HEAVY,
    VERIFY_BOUNDARY,
]

ACTOR_INFO_FILE = "actor_info.json"

CURRENT_VERSION = "2.0"
SUPPORTED_VERSIONS = ["2.0"]


@dataclass
class Envelope:
    """
    Immutable signed communication unit.

    Each envelope represents a single communication event
    between two actors.
    """

    from_: str = field(metadata={"alias": "from"})

    # Message, decision, response, verification result, etc.
    payload: str

    # Epoch at which the envelop is formed
    epoch: int

    # Code represents the error category during an
    # envelope formation event
    status_code: int = RESULT_OK

    # run_id acts as a reference for access token
    run_id: str = ""

    # Signature of the canonical envelope representation.
    signature: str = ""

    # Previous envelope in the provenance chain.
    parent_envelope: list[Envelope] | None = None

    # To whom the envelope is forwared to explicity
    to: str = ""

    # Hash of the envelope for integrity verification
    hash: str = ""

    def add_envelope(self, new_envelope: Envelope | None):
        if new_envelope is None:
            return

        if self.parent_envelope is None:
            self.parent_envelope = [new_envelope]
        else:
            # Check to ensure we don't end up adding
            # duplicate envelopes
            if new_envelope not in self.parent_envelope:
                self.parent_envelope.append(new_envelope)


def dump_workflow(workflow: IntentWorkflow) -> dict:
    """
    Converts an IntentWorkflow object back into a raw dictionary,
    ready for JSON serialization.
    """
    return {
        "id": workflow.id,
        "type": workflow.type,
        "version": workflow.version,
        "info": workflow.info,
        "envelope": dump_envelope(workflow.envelope),
    }


def dump_envelope(envelope: Envelope | None) -> dict | None:
    """
    Recursively turns a proper Envelope object into a raw dict,
    safely handling the DAG of parent envelopes and mapping 'from_' back to 'from'.
    """
    if envelope is None:
        return None

    result = {
        "from": envelope.from_,  # <-- CRITICAL: Map from_ back to from
        "payload": envelope.payload,
        "epoch": envelope.epoch,
        "status_code": envelope.status_code,
        "run_id": envelope.run_id,
        "to": envelope.to,
        "hash": envelope.hash,
        "signature": envelope.signature,
    }

    # Handle the recursive DAG properly
    if envelope.parent_envelope:
        result["parent_envelope"] = [dump_envelope(parent) for parent in envelope.parent_envelope]
    else:
        result["parent_envelope"] = None

    return result

def load_workflow(data: dict | IntentWorkflow) -> IntentWorkflow:
    """
    Converts a raw dictionary (from JSON serialization) back into 
    an IntentWorkflow object.
    """
    if isinstance(data, IntentWorkflow):
        return data

    data_copy = dict(data)
    data_copy["envelope"] = load_envelope(data_copy.get("envelope"))
    return IntentWorkflow(**data_copy)


def load_envelope(data: dict | Envelope | None) -> Envelope | None:
    """
    Recursively turns a raw dict back into a proper Envelope object,
    handling parent envelopes and mapping 'from' back to 'from_'.
    """
    if data is None or isinstance(data, Envelope):
        return data
    if not isinstance(data, dict):
        raise TypeError(f"Expected dict or Envelope, got {type(data)}")

    data_copy = dict(data)

    # CRITICAL: Map network key 'from' back to Python attribute 'from_'
    if "from" in data_copy:
        data_copy["from_"] = data_copy.pop("from")

    # Handle the recursive DAG properly
    parents = data_copy.get("parent_envelope")
    if parents:
        data_copy["parent_envelope"] = [load_envelope(parent) for parent in parents]
    else:
        data_copy["parent_envelope"] = None

    return Envelope(**data_copy)

@dataclass
class IntentWorkflow:
    """
    Complete record of an intent execution.

    The envelope field points to the latest envelope
    in the chain.

    The entire workflow can be reconstructed by
    recursively traversing parent_envelope.
    """

    id: str
    type: str
    version: str

    # Workflow-level information.
    info: dict[str, Any] = field(default_factory=dict)

    # Latest envelope in the workflow.
    envelope: Envelope | None = None

    def set_envelope(self, envelope: Envelope):
        self.envelope = envelope

    def get_latest_envelope(self) -> Envelope:
        """Returns the latest envelope (the tip) from the workflow."""
        if self.envelope is None:
            raise ValueError("workflow does not contain an envelope")
        return self.envelope

    def get_root_envelope(self) -> Envelope:
        """
        Returns the single root envelope (the original human intent).
        Since all branches originate from the same single root, we
        can efficiently walk straight up the first parent branch.
        """
        if self.envelope is None:
            raise ValueError("workflow does not contain an envelope")

        current = self.envelope
        # Traverse up until a node has no parents
        while current.parent_envelope:
            current = current.parent_envelope[0]

        return current

    def get_root_envelope_actor(self) -> str:
        """
        Returns the actor ID of the root envelope
        """
        root_envelope = self.get_root_envelope()
        return root_envelope.from_

    def get_latest_envelope_actor(self) -> str:
        """
        Returns the actor ID of the latest envelope
        """
        latest_envelope = self.get_latest_envelope()
        return latest_envelope.from_

    def serialize(self) -> str:
        """
        Serializes the workflow into a dictionary representation.
        """
        raw_dict = dump_workflow(self)
        return json.dumps(raw_dict, separators=(",", ":"))

    def unwrap(self) -> list[Envelope]:
        """
        Flattens the provenance graph into a unique list of envelopes.
        Returns them in reverse chronological order (newest to oldest)
        using Breadth-First Search (BFS).
        """
        if not self.envelope:
            return []

        unwrapped: list[Envelope] = []
        visited_hashes = set()
        queue = [self.envelope]

        while queue:
            current = queue.pop(0)

            if current.hash not in visited_hashes:
                visited_hashes.add(current.hash)
                unwrapped.append(current)

                if current.parent_envelope:
                    queue.extend(current.parent_envelope)

        unwrapped.sort(key=lambda env: (env.epoch, env.hash), reverse=True)
        return unwrapped


@dataclass
class AgentCard:
    type: str
    id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    policy: str = ""


@dataclass
class UserCard:
    type: str
    id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActorRegistryEntry:
    actor_id: str
    actor_name: str
    actor_card_id: str

class AgentNotWhitelistedError(Exception):
    """Raised when an agent is not whitelisted in the Admin server."""
    pass

class CoCAVerificationError(Exception):
    """Raised when a CoCA verification fails."""
    pass