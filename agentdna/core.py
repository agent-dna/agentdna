import base64
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from .card import build_actor_card_payload, build_user_card_payload
from .config import (
    ActorRegistryEntry,
    get_actor_registry_entry,
    get_default_config_dir,
    upsert_actor_registry_entry,
)
from .id import get_agent_card_id, get_user_card_id
from .provenance import Provenance
from .types import (
    ACTOR_TYPE_AGENT,
    ACTOR_TYPE_HUMAN,
    CURRENT_VERSION,
    VERIFY_HEAVY,
    VERIFY_LIGHT,
    Actor,
    AgentCard,
    Envelope,
    HandleResult,
    IntentWorkflow,
    VerificationResult,
    supported_actors,
    supported_verification_modes,
)
from .verifier import (
    verify_heavy,
    verify_light,
)


class AgentDNA:
    def __init__(
        self,
        name: str,
        type: str,
        provenance_layer_url: str = "https://chain-connector-2.rubix.net",
        api_key: str = "",
        config_dir: str = "",
        metadata: dict[str, Any] | None = None,
        verification_mode: str = VERIFY_LIGHT,
        agent_policy_file: Path | None = None,
        skip_actor_id_registration: bool = False,
    ):
        if name == "":
            raise NameError("'name' attribute cannot be empty")

        if type not in supported_actors:
            raise ValueError(
                f"provided actor type {type} is not supported. Supported actors are {supported_actors}"
            )

        if verification_mode not in supported_verification_modes:
            raise ValueError(
                f"unsupported verification mode: "
                f"{verification_mode}. "
                f"Supported modes: "
                f"{supported_verification_modes}"
            )
        self.verification_mode = verification_mode

        self.api_key = api_key

        self.config_dir = config_dir if config_dir else get_default_config_dir()
        os.makedirs(
            self.config_dir,
            exist_ok=True,
        )

        self.provenance = Provenance(
            name=name,
            provenance_url=provenance_layer_url,
            api_key=api_key,
            config_path=self.config_dir,
        )

        self.type = type
        self.name = name
        self.metadata = metadata or {}
        self.__agent_policy_path = agent_policy_file

        # Check from local config dir, if the actor card
        # has already been created
        entry = get_actor_registry_entry(
            self.config_dir,
            self.get_actor_id(),
        )

        if entry is not None:
            self.card_id = entry.actor_card_id
        elif self.type == ACTOR_TYPE_HUMAN:
            self.card_id = self.create_user_card()
        else:
            self.card_id = ""

        # Beta: Register creds corresponding to Actor ID
        if not skip_actor_id_registration:
            if self.type == ACTOR_TYPE_AGENT:
                self.__register_agent()

            if self.type == ACTOR_TYPE_HUMAN:
                self.__register_user()

    def __validate_api_key(self) -> None:
        if self.api_key == "":
            raise ValueError("api_key is required for actor registration")

    def __register_user(self) -> None:
        """
        Registers the current user with
        the provenance layer.
        """

        self.__validate_api_key()

        response = requests.post(
            urljoin(self.provenance.provenance_url, "/core/v1/register-user"),
            json={
                "user_id": self.get_actor_id(),
            },
            headers={
                "X-API-Key": self.api_key,
            },
            timeout=300,
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("status"):
            raise RuntimeError(f"user registration failed: {data.get('message', '')}")

    def __register_agent(self) -> None:
        """
        Registers the current agent with
        the provenance layer.
        """

        self.__validate_api_key()

        policy_path = self.__agent_policy_path
        if policy_path is None:
            raise ValueError("agent policy path is not defined")

        if not policy_path.exists():
            raise FileNotFoundError(f"policy file not found: {policy_path}")

        with open(
            policy_path,
            "rb",
        ) as fp:
            response = requests.post(
                urljoin(self.provenance.provenance_url, "/core/v1/register-agent"),
                data={
                    "agent_name": self.name,
                    "agent_id": self.get_actor_id(),
                },
                files={
                    "policy": (
                        policy_path.name,
                        fp,
                    )
                },
                headers={
                    "X-API-Key": self.api_key,
                },
                timeout=300,
            )

        response.raise_for_status()

        data = response.json()

        if not data.get("status"):
            raise RuntimeError(f"agent registration failed: {data.get('message', '')}")

    def get_actor_id(self) -> str:
        return self.provenance.provenance_id

    def create_user_card(self) -> str:
        if self.type != ACTOR_TYPE_HUMAN:
            raise ValueError("only human actors can create user cards")

        user_card_id = get_user_card_id(self.get_actor_id())

        entry = get_actor_registry_entry(
            self.config_dir,
            self.get_actor_id(),
        )

        if entry is not None:
            return entry.actor_card_id

        payload = build_user_card_payload(
            user_id=self.get_actor_id(),
        )

        self.provenance.create_new_provenance_card(
            card_id=user_card_id,
            card_info=json.dumps(payload),
        )

        upsert_actor_registry_entry(
            self.config_dir,
            ActorRegistryEntry(
                actor_id=self.get_actor_id(),
                actor_name=self.name,
                actor_card_id=user_card_id,
            ),
        )

        return user_card_id

    def create_agent_card_by_id(self, agent_id: str, policy_file: str | Path) -> str:
        """
        Creates an Agent Card by passing its DID

        Only human actors are allowed to create
        agent cards.

        It skips actor_info.json config update
        """

        if self.type != ACTOR_TYPE_HUMAN:
            raise ValueError("only human actors can create agent cards")

        policy_path = Path(policy_file)

        if not policy_path.exists():
            raise FileNotFoundError(f"policy file does not exist: {policy_path}")

        with open(
            policy_path,
            "rb",
        ) as fp:
            policy_b64 = base64.b64encode(fp.read()).decode("utf-8")

        agent_card_id = get_agent_card_id(agent_id=agent_id)

        payload = build_actor_card_payload(
            actor_id=agent_id,
            policy=policy_b64,
        )

        try:
            self.provenance.create_new_provenance_card(
                card_id=agent_card_id,
                card_info=json.dumps(payload),
            )
        except Exception as exc:
            raise Exception(f"failed to create agent card, err: {exc}") from exc

        return agent_card_id

    def create_agent_card(
        self,
        agent: "AgentDNA",
        policy_file: str | Path,
    ) -> str:
        """
        Creates an Agent Card for the supplied agent.

        Only human actors are allowed to create
        agent cards.
        """

        if self.type != ACTOR_TYPE_HUMAN:
            raise ValueError("only human actors can create agent cards")

        if agent.type != ACTOR_TYPE_AGENT:
            raise ValueError("provided AgentDNA instance is not of type agent")

        policy_path = Path(policy_file)

        if not policy_path.exists():
            raise FileNotFoundError(f"policy file does not exist: {policy_path}")

        with open(
            policy_path,
            "rb",
        ) as fp:
            policy_b64 = base64.b64encode(fp.read()).decode("utf-8")

        agent_card_id = get_agent_card_id(agent.get_actor_id())

        entry = get_actor_registry_entry(
            self.config_dir,
            agent.get_actor_id(),
        )

        if entry is not None:
            agent.card_id = entry.actor_card_id
            return agent.card_id

        payload = build_actor_card_payload(
            actor_id=agent.get_actor_id(),
            policy=policy_b64,
        )

        try:
            self.provenance.create_new_provenance_card(
                card_id=agent_card_id,
                card_info=json.dumps(payload),
            )
        except Exception as exc:
            raise Exception(f"failed to create agent card, err: {exc}") from exc

        agent.card_id = agent_card_id

        upsert_actor_registry_entry(
            self.config_dir,
            ActorRegistryEntry(
                actor_id=agent.get_actor_id(),
                actor_name=agent.name,
                actor_card_id=agent_card_id,
            ),
        )

        return agent_card_id

    def update_agent_policy_by_id(self, agent_id: str, policy_file: str | Path):
        if self.type != ACTOR_TYPE_HUMAN:
            raise ValueError("only human actors can update agent policies")

        policy_path = Path(policy_file)

        if not policy_path.exists():
            raise FileNotFoundError(f"policy file does not exist: {policy_path}")

        with open(policy_path, "rb") as fp:
            policy_b64 = base64.b64encode(fp.read()).decode("utf-8")

        history = self.provenance.provenance_card_history(actor_id=agent_id)

        if len(history) == 0:
            raise ValueError(f"agent card not found for {agent_id}")

        latest_data = history[-1]["data"]

        actor_card_dict = json.loads(latest_data)

        actor_card = AgentCard(**actor_card_dict)
        actor_card.policy = policy_b64

        try:
            agent_card_id = get_agent_card_id(agent_id=agent_id)
            self.provenance.append_to_provenance_card(
                card_id=agent_card_id,
                card_info=json.dumps(asdict(actor_card)),
            )
        except Exception as exc:
            raise RuntimeError(f"failed to update agent {agent_id} policy: {exc}") from exc

    def update_agent_policy(
        self,
        agent: "AgentDNA",
        policy_file: str | Path,
    ):
        if agent.type != ACTOR_TYPE_AGENT:
            raise ValueError("provided AgentDNA instance is not of type agent")

        self.update_agent_policy_by_id(agent.get_actor_id(), policy_file=policy_file)

    def create_workflow_provenance(
        self,
        workflow: IntentWorkflow,
    ) -> str:
        if self.type != ACTOR_TYPE_HUMAN:
            raise RuntimeError("only users can create workflow provenance")

        if self.card_id == "":
            raise ValueError("failed to create workflow provenance as user Card is not created")

        if workflow.envelope is None:
            raise ValueError("workflow does not contain an envelope")

        try:
            workflow_card_id = self.provenance.create_new_child_provenance_card(
                parent_card_id=self.card_id,
                card_info=json.dumps(asdict(workflow)),
            )

            return workflow_card_id
        except Exception as exc:
            raise RuntimeError(f"failed create workflow provenance, err: {exc}") from exc

    def build(
        self,
        recipient_actor_id: str,
        recipient_actor_name: str,
        recipient_actor_type: str,
        payload: str,
        verification_result: VerificationResult | None = None,
        workflow: IntentWorkflow | None = None,
        remarks: str = "",
        from_actor: Actor | None = None,
    ) -> IntentWorkflow:
        """
        Creates and signs a new envelope and
        returns the IntentWorklow
        """

        if recipient_actor_type not in supported_actors:
            raise ValueError(
                f"unsupported actor type: {recipient_actor_type},"
                f"supported actor types: {supported_actors}"
            )

        from_ = from_actor or Actor(
            id=self.get_actor_id(),
            name=self.name,
            type=self.type,
        )

        to = Actor(
            id=recipient_actor_id,
            name=recipient_actor_name,
            type=recipient_actor_type,
        )

        parent_envelope = None

        if workflow:
            parent_envelope = workflow.envelope

        envelope = Envelope(
            from_=from_,
            to=to,
            payload=payload,
            metadata=self.metadata or {},
            parent_envelope=parent_envelope,
            epoch=int(datetime.now(timezone.utc).timestamp()),
        )

        if verification_result:
            envelope.issues = verification_result.issues

        signature = self.provenance.sign_envelope(envelope)

        envelope.signature = signature

        if workflow is None:
            workflow = IntentWorkflow(
                type="intent_workflow",
                version=CURRENT_VERSION,
                remarks=remarks,
                envelope=envelope,
            )
        else:
            workflow.envelope = envelope

        return workflow

    def handle(
        self,
        workflow: IntentWorkflow,
    ) -> HandleResult:
        """
        Verifies an incoming workflow and returns
        the latest envelope.
        """
        if workflow.envelope is None:
            raise ValueError("workflow does not contain an envelope")

        if self.verification_mode == VERIFY_LIGHT:
            verification = verify_light(
                self.provenance,
                workflow,
            )
        elif self.verification_mode == VERIFY_HEAVY:
            verification = verify_heavy(
                self.provenance,
                workflow,
            )
        else:
            raise ValueError(f"unsupported verification mode {self.verification_mode}")

        return HandleResult(
            workflow=workflow,
            envelope=workflow.envelope,
            verification=verification,
        )
