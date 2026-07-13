from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List

ACTOR_TYPE_AGENT = "agent"
ACTOR_TYPE_HUMAN = "human"
ACTOR_TYPE_APP = "app"
supported_actors = [ACTOR_TYPE_AGENT, ACTOR_TYPE_HUMAN, ACTOR_TYPE_APP]

VERIFY_LIGHT = "light"
VERIFY_HEAVY = "heavy"
supported_verification_modes = [
    VERIFY_LIGHT,
    VERIFY_HEAVY,
]

ACTOR_INFO_FILE = "actor_info.json"
MODEL_EMBEDDINGS_DIR = "embeddings_cache"

CURRENT_VERSION = "1.0"
SUPPORTED_VERSIONS = ["1.0"]


@dataclass
class Actor:
    """
    Represents an actor participating in the workflow.
    """

    id: str
    name: str
    type: str

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Envelope:
    """
    Immutable signed communication unit.

    Each envelope represents a single communication event
    between two actors.
    """

    from_: Actor = field(metadata={"alias": "from"})
    to: Actor

    # Message, decision, response, verification result, etc.
    payload: str

    # Epoch at which the envelop is formed
    epoch: int

    # Additional information that may evolve over time.
    metadata: dict[str, Any] = field(default_factory=dict)

    # Signature of the canonical envelope representation.
    signature: str = ""

    # List of issues being observered via CoCA or CBAC layers
    issues: List[Issue] = field(default_factory=list)

    # Previous envelope in the provenance chain.
    parent_envelope: Envelope | None = None


@dataclass
class IntentWorkflow:
    """
    Complete record of an intent execution.

    The envelope field points to the latest envelope
    in the chain.

    The entire workflow can be reconstructed by
    recursively traversing parent_envelope.
    """

    type: str
    version: str
    remarks: str

    # Workflow-level information.
    info: dict[str, Any] = field(default_factory=dict)

    # Latest envelope in the workflow.
    envelope: Envelope | None = None


@dataclass
class Issue:
    depth: int
    reason: str


@dataclass
class VerificationResult:
    valid: bool
    chain_depth: int
    issues: list[Issue] = field(default_factory=list)


@dataclass
class HandleResult:
    workflow: IntentWorkflow
    envelope: Envelope
    verification: VerificationResult


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
