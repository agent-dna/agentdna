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
from .error import (
    COCA_VERIFICATION_FAILED_BOUNDARY,
    COCA_VERIFICATION_FAILED_HEAVY,
    COCA_VERIFICATION_FAILED_LIGHT,
    RESULT_OK,
)
from .helpers import _hash_content
from .id import (
    get_agent_card_id,
    get_intent_workflow_id,
    get_user_card_id,
)
from .log import configure_logging, get_logger
from .provenance import Provenance
from .types import (
    ACTOR_TYPE_AGENT,
    ACTOR_TYPE_TOOL,
    ACTOR_TYPE_USER,
    CURRENT_VERSION,
    VERIFY_BOUNDARY,
    VERIFY_HEAVY,
    VERIFY_LIGHT,
    AgentCard,
    Envelope,
    IntentWorkflow,
    supported_actors,
    supported_verification_modes,
)
from .verifier import (
    verify_boundary,
    verify_heavy,
    verify_light,
)


class AgentDNA:
    def __init__(
        self,
        name: str,
        type: str,
        provenance_layer_url: str = "https://chain-connector-2.rubix.net",
        admin_server_url: str = "https://agentdna-admin.rubix.net",
        api_key: str = "",
        config_dir: str = "",
        metadata: dict[str, Any] | None = None,
        verification_mode: str = VERIFY_BOUNDARY,
        agent_policy_file: Path | None = None,
        skip_actor_id_registration: bool = False,
        log_level: str = "INFO",
        log_format: str = "text",
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

        configure_logging(log_level, log_format)
        self.logger = get_logger("agentdna").bind(actor_name=name, actor_type=type)

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
            logger=self.logger,
            actor_type=type,
        )
        self.agentdna_admin_url = admin_server_url

        self.logger = self.provenance.logger

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
        elif self.type == ACTOR_TYPE_USER:
            self.card_id = self.create_user_card()
        else:
            self.card_id = ""

        # Beta: Register creds corresponding to Actor ID
        if not skip_actor_id_registration:
            if self.type == ACTOR_TYPE_AGENT:
                self.__register_agent()

            if self.type == ACTOR_TYPE_USER:
                self.__register_user()

            if self.type == ACTOR_TYPE_TOOL:
                self.__register_tool()

        self.logger.info("agentdna.init.success", card_id=self.card_id)

    def __validate_api_key(self) -> None:
        if self.api_key == "":
            raise ValueError("api_key is required for actor registration")

    def __register_user(self) -> None:
        """
        Registers the current user with
        the provenance layer.
        """

        self.__validate_api_key()

        try:
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
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "agentdna.register_user.failed",
                status_code=getattr(exc.response, "status_code", None),
                error=str(exc),
            )
            raise

        data = response.json()

        if not data.get("status"):
            error_msg = data.get("message", "")
            self.logger.error("agentdna.register_user.failed", error=error_msg)
            raise RuntimeError(f"user registration failed: {error_msg}")

        self.logger.info("agentdna.register_user.success")

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

        try:
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
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "agentdna.register_agent.failed",
                policy_path=str(policy_path),
                status_code=getattr(exc.response, "status_code", None),
                error=str(exc),
            )
            raise

        data = response.json()

        if not data.get("status"):
            error_msg = data.get("message", "")
            self.logger.error(
                "agentdna.register_agent.failed",
                policy_path=str(policy_path),
                error=error_msg,
            )
            raise RuntimeError(f"agent registration failed: {error_msg}")

        self.logger.info("agentdna.register_agent.success", policy_path=str(policy_path))

    def __register_tool(self) -> None:
        """
        Registers the current tool with
        the provenance layer.
        """

        try:
            response = requests.post(
                urljoin(self.provenance.provenance_url, "/core/v1/register-tool"),
                json={
                    "tool_name": self.name,
                    "tool_id": self.get_actor_id(),
                },
                timeout=300,
            )

            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            self.logger.error(
                "agentdna.register_tool.failed",
                status_code=getattr(exc.response, "status_code", None),
                error=str(exc),
            )
            raise

        data = response.json()

        if not data.get("status"):
            error_msg = data.get("message", "")
            self.logger.error("agentdna.register_tool.failed", error=error_msg)
            raise RuntimeError(f"tool registration failed: {error_msg}")

        self.logger.info("agentdna.register_tool.success")

    def get_actor_id(self) -> str:
        return self.provenance.provenance_id

    def create_user_card(self) -> str:
        if self.type != ACTOR_TYPE_USER:
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

        try:
            self.provenance.create_new_provenance_card(
                card_id=user_card_id,
                card_info=json.dumps(payload),
            )
        except Exception as exc:
            self.logger.error("agentdna.create_user_card.failed", error=str(exc))
            raise

        upsert_actor_registry_entry(
            self.config_dir,
            ActorRegistryEntry(
                actor_id=self.get_actor_id(),
                actor_name=self.name,
                actor_card_id=user_card_id,
            ),
        )

        self.logger.info("agentdna.create_user_card.success", card_id=user_card_id)
        return user_card_id

    def create_agent_card_by_id(self, agent_id: str, policy_file: str | Path) -> str:
        """
        Creates an Agent Card by passing its DID

        Only human actors are allowed to create
        agent cards.

        It skips actor_info.json config update
        """

        if self.type != ACTOR_TYPE_USER:
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
            self.logger.error(
                "agentdna.create_agent_card_by_id.failed",
                agent_id=agent_id,
                error=str(exc),
            )
            raise Exception(f"failed to create agent card, err: {exc}") from exc

        self.logger.info(
            "agentdna.create_agent_card_by_id.success",
            agent_id=agent_id,
            card_id=agent_card_id,
        )
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

        if self.type != ACTOR_TYPE_USER:
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
            self.logger.error(
                "agentdna.create_agent_card.failed",
                agent_id=agent.get_actor_id(),
                error=str(exc),
            )
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

        self.logger.info(
            "agentdna.create_agent_card.success",
            agent_id=agent.get_actor_id(),
            card_id=agent_card_id,
        )
        return agent_card_id

    def update_agent_policy_by_id(self, agent_id: str, policy_file: str | Path):
        if self.type != ACTOR_TYPE_USER:
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
            self.logger.error(
                "agentdna.update_agent_policy.failed",
                agent_id=agent_id,
                error=str(exc),
            )
            raise RuntimeError(f"failed to update agent {agent_id} policy: {exc}") from exc

    def update_agent_policy(
        self,
        agent: "AgentDNA",
        policy_file: str | Path,
    ):
        if agent.type != ACTOR_TYPE_AGENT:
            raise ValueError("provided AgentDNA instance is not of type agent")

        self.update_agent_policy_by_id(agent.get_actor_id(), policy_file=policy_file)

    def record(
        self,
        workflow: IntentWorkflow,
    ) -> str:
        if workflow.envelope is None:
            raise ValueError("workflow does not contain an envelope")

        root_user_id = workflow.get_root_envelope_actor()
        user_card_id = get_user_card_id(user_id=root_user_id)

        try:
            workflow_card_id = self.provenance.create_new_child_provenance_card(
                parent_card_id=user_card_id,
                card_info=workflow.serialize(),
                child_nft_id=workflow.id
            )

            self.logger.info(
                "agentdna.record.success",
                actor_name=self.name,
                actor_id=self.get_actor_id(),
                user_card_id=user_card_id,
                workflow_card_id=workflow_card_id,
            )

            return workflow_card_id
        except Exception as exc:
            self.logger.error("agentdna.record.failed", error=str(exc))
            raise RuntimeError(f"failed create workflow provenance, err: {exc}") from exc

    def build(
        self,
        payload: str,
        previous_workflows: IntentWorkflow | list[IntentWorkflow] | None = None,
        recipient_id: str = "",
        verification_code: int = RESULT_OK,
    ) -> IntentWorkflow:
        """
        Creates and signs a new envelope and
        returns the IntentWorkflow
        """
        epoch = int(datetime.now(timezone.utc).timestamp())

        self.logger.info(
            "agentdna.build.start",
            recipient_id=recipient_id,
            code=verification_code,
            epoch=epoch,
        )

        workflow_id = ""
        if previous_workflows is not None:
            if isinstance(previous_workflows, IntentWorkflow):
                if previous_workflows.id == "":
                    raise ValueError("previous_workflows.id cannot be empty")

                workflow_id = previous_workflows.id
            elif isinstance(previous_workflows, list) and len(previous_workflows) > 0:
                for prev_workflow in previous_workflows:
                    if prev_workflow.id == "":
                        raise ValueError("previous_workflows.id cannot be empty")

                workflow_id = previous_workflows[0].id
            else:
                raise ValueError(
                    "previous_workflows must be an IntentWorkflow or a list of IntentWorkflows"
                )
        else:
            # The workflow_id is generated based on card ID of actor type "user" only.
            # This is based on the idea that only users initiates the intent.
            if self.type != ACTOR_TYPE_USER:
                raise ValueError("workflow_id can only be generated for actors of type 'user'")

            if self.card_id == "":
                raise ValueError("card_id for user cannot be empty")

            workflow_id = get_intent_workflow_id(self.card_id)

        current_envelope = Envelope(
            from_=self.get_actor_id(),
            payload=payload,
            epoch=epoch,
            run_id="",  # TODO: Define IdP integration logic
            status_code=verification_code,
            to=recipient_id,
        )

        if previous_workflows:
            if isinstance(previous_workflows, IntentWorkflow):
                previous_workflows = [previous_workflows]

            for prev_workflow in previous_workflows:
                if prev_workflow.envelope:
                    current_envelope.add_envelope(prev_workflow.envelope)

        current_envelope.hash = _hash_content(current_envelope)

        signature = self.provenance.sign_envelope(current_envelope)
        current_envelope.signature = signature

        new_workflow = IntentWorkflow(
            id=workflow_id,
            type="intent_workflow",
            version=CURRENT_VERSION,
        )
        new_workflow.set_envelope(current_envelope)

        self.logger.info(
            "agentdna.build.success",
            recipient_id=recipient_id,
            code=verification_code,
            epoch=epoch,
        )

        return new_workflow

    def verify(
        self,
        workflow: IntentWorkflow,
    ) -> int:
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
            if not verification:
                self.logger.warning(
                    "agentdna.verify.failed",
                    verification_mode=self.verification_mode,
                    code=COCA_VERIFICATION_FAILED_LIGHT,
                )
                return COCA_VERIFICATION_FAILED_LIGHT

        elif self.verification_mode == VERIFY_HEAVY:
            verification = verify_heavy(
                self.provenance,
                workflow,
            )
            if not verification:
                self.logger.warning(
                    "agentdna.verify.failed",
                    verification_mode=self.verification_mode,
                    code=COCA_VERIFICATION_FAILED_HEAVY,
                )
                return COCA_VERIFICATION_FAILED_HEAVY

        elif self.verification_mode == VERIFY_BOUNDARY:
            verification = verify_boundary(
                self.provenance,
                workflow,
            )
            if not verification:
                self.logger.warning(
                    "agentdna.verify.failed",
                    verification_mode=self.verification_mode,
                    code=COCA_VERIFICATION_FAILED_BOUNDARY,
                )
                return COCA_VERIFICATION_FAILED_BOUNDARY
        else:
            raise ValueError(f"unsupported verification mode {self.verification_mode}")

        self.logger.info("agentdna.verify.success", verification_mode=self.verification_mode)
        return RESULT_OK
