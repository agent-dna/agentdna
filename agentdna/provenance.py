import json
import os
from typing import Any

from rubix.client import RubixClient
from rubix.did import fast_signature_verify
from rubix.querier import Querier
from rubix.signer import Signer
from structlog.typing import FilteringBoundLogger

from .config import get_default_config_dir
from .helpers import _hash_content, canonicalize_envelope
from .id import get_agent_card_id
from .log import configure_logging, get_logger
from .types import Envelope


class Provenance:
    def __init__(
        self,
        name: str,
        provenance_url: str = "https://chain-connector-2.rubix.net",
        api_key: str = "",
        config_path: str = "",
        timeout_in_seconds: int = 10000,
        logger: FilteringBoundLogger | None = None,
        actor_type: str = "",
    ):
        if name == "":
            raise NameError("'name' attribute cannot be empty")

        if provenance_url == "":
            raise ValueError("'provenance_url' attribute cannot be empty")

        self.provenance_url = provenance_url

        provenance_client = RubixClient(
            node_url=provenance_url, timeout=timeout_in_seconds, api_key=api_key
        )

        self.config_dir = config_path if config_path else get_default_config_dir()
        os.makedirs(
            self.config_dir,
            exist_ok=True,
        )

        self.provenance_executor = Signer(
            rubixClient=provenance_client, alias=name, config_path=self.config_dir
        )

        self.provenance_querier = Querier(
            rubixClient=provenance_client,
        )

        self.provenance_id = self.provenance_executor.did

        if logger is not None:
            self.logger = logger
        else:
            configure_logging()
            self.logger = get_logger("agentdna.provenance").bind(
                actor_name=name, actor_type=actor_type or "unknown"
            )

        self.logger = self.logger.bind(actor_id=self.provenance_id)

    def create_new_provenance_card(
        self,
        card_id: str,
        card_info: str,
        card_value: float = 0.001,
    ):
        self.logger.info("provenance_card.create.start", card_id=card_id, card_value=card_value)
        try:
            result = self.provenance_executor.deploy_nft(
                nft_id=card_id, nft_value=card_value, nft_data=card_info
            )
            self.logger.info(
                "provenance_card.create.success", card_id=card_id, card_value=card_value
            )
            return result
        except Exception as exc:
            self.logger.error(
                "provenance_card.create.failed",
                card_id=card_id,
                card_value=card_value,
                error=str(exc),
            )
            raise

    def create_new_child_provenance_card(self, parent_card_id: str, card_info: str, child_nft_id: str):
        try:
            response = self.provenance_executor.create_child_nft(
                parent_nft_address=parent_card_id,
                nft_data=card_info,
                child_nft_id=child_nft_id,
            )
        except Exception as exc:
            self.logger.error(
                "provenance_card.create_child.failed",
                parent_card_id=parent_card_id,
                error=str(exc),
            )
            raise

        if response.get("error") is not None:
            error_msg = response["error"]
            self.logger.error(
                "provenance_card.create_child.failed",
                parent_card_id=parent_card_id,
                error=error_msg,
            )
            raise Exception(f"Child NFT creation failed: {error_msg}")
        else:
            if len(response["child_nfts"]) == 0:
                self.logger.error(
                    "provenance_card.create_child.failed",
                    parent_card_id=parent_card_id,
                    error="no_card_created",
                )
                raise Exception(
                    f"Child Provenance Card creation failed for parent card {parent_card_id}, no card was created"
                )

            child_provenance_card_id = response["child_nfts"][0].get("childNFTId")
            if child_provenance_card_id is None:
                self.logger.error(
                    "provenance_card.create_child.failed",
                    parent_card_id=parent_card_id,
                    error="unable_to_extract_id",
                )
                raise Exception(
                    f"Child Provenance Card creation failed for parent card "
                    f"{parent_card_id}, unable to extract id from "
                    f"{response['child_nfts']}"
                )

            self.logger.info(
                "provenance_card.create_child.success",
                parent_card_id=parent_card_id,
                card_id=child_provenance_card_id,
            )
            return child_provenance_card_id

    def append_to_provenance_card(
        self,
        card_id: str,
        card_info: str,
    ):
        """
        Appends info to an existing card
        """
        self.logger.info("provenance_card.append.start", card_id=card_id)
        try:
            result = self.provenance_executor.execute_nft(
                nft_address=card_id,
                nft_data=card_info,
            )
            self.logger.info("provenance_card.append.success", card_id=card_id)
            return result
        except Exception as exc:
            self.logger.error("provenance_card.append.failed", card_id=card_id, error=str(exc))
            raise

    def provenance_card_history(self, actor_id: str):
        """
        List the set of records written on a Card
        """
        if actor_id == "":
            self.logger.error("provenance_card.history.failed", error="empty_actor_id")
            raise ValueError("agent_id cannot be empty")

        if not actor_id.startswith("bafy"):
            self.logger.error(
                "provenance_card.history.failed",
                actor_id=actor_id,
                error="invalid_actor_id_format",
            )
            raise ValueError(f"invalid agent_id provided: {actor_id}, it should start with 'bafy'")

        agent_card_id = get_agent_card_id(agent_id=actor_id)

        try:
            result = self.provenance_querier.get_nft_states(nft_address=agent_card_id)
            self.logger.info(
                "provenance_card.history.success",
                actor_id=actor_id,
                count=len(result),
            )
            return result
        except Exception as exc:
            self.logger.error(
                "provenance_card.history.failed",
                actor_id=actor_id,
                error=str(exc),
            )
            raise

    def get_latest_provenance_record(
        self,
        actor_id: str,
    ) -> dict[str, Any]:
        """
        Returns the latest provenance record
        associated with the supplied actor.
        """
        history = self.provenance_card_history(actor_id=actor_id)

        if len(history) == 0:
            self.logger.error(
                "provenance_card.latest.failed",
                actor_id=actor_id,
                error="no_records_found",
            )
            raise ValueError(f"no provenance records found for actor {actor_id}")

        latest_record = history[-1]

        data = latest_record.get("data")

        if data is None:
            self.logger.error(
                "provenance_card.latest.failed",
                actor_id=actor_id,
                error="no_data_field",
            )
            raise ValueError("latest provenance record does not contain data")

        try:
            result = json.loads(data)
            self.logger.info(
                "provenance_card.latest.success",
                actor_id=actor_id,
            )
            return result
        except Exception as exc:
            self.logger.error(
                "provenance_card.latest.failed",
                actor_id=actor_id,
                error=str(exc),
            )
            raise

    def sign_envelope(
        self,
        envelope: Envelope,
    ) -> str:
        """
        Signs an envelope and returns
        a hex encoded signature.
        """

        self.logger.info(
            "envelope.sign.start",
            envelope_from=envelope.from_,
            epoch=envelope.epoch,
        )
        try:
            keypair = self.provenance_executor.get_keypair()

            message = canonicalize_envelope(envelope)

            signature_bytes = keypair.sign(message.encode("utf-8"))

            signature_hex = signature_bytes.hex()
            self.logger.info(
                "envelope.sign.success",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                signature=signature_hex,
            )
            return signature_hex
        except Exception as exc:
            self.logger.error(
                "envelope.sign.failed",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                error=str(exc),
            )
            raise

    def verify_envelope(
        self,
        envelope: Envelope,
    ) -> bool:
        """
        Verifies an envelope signature against the supplied actor DID,
        and validates its intrinsic Merkle hash.
        """
        try:
            if envelope.from_ == "":
                self.logger.error(
                    "envelope.verify.missing_actor",
                    envelope_from=envelope.from_,
                    epoch=envelope.epoch,
                )
                return False

            if envelope.signature == "":
                self.logger.error(
                    "envelope.verify.missing_signature",
                    envelope_from=envelope.from_,
                    epoch=envelope.epoch,
                )
                return False

            expected_hash = _hash_content(envelope)

            if envelope.hash != expected_hash:
                self.logger.error(
                    "envelope.verify.hash_mismatch",
                    envelope_from=envelope.from_,
                    expected=expected_hash,
                    actual=envelope.hash,
                )
                return False

            signature_bytes = bytes.fromhex(envelope.signature)
        except ValueError:
            self.logger.error(
                "envelope.verify.invalid_signature_format",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                raw_signature=envelope.signature,
            )
            return False

        message = canonicalize_envelope(envelope)

        try:
            is_valid = fast_signature_verify(
                rubixNodeBaseUrl=self.provenance_url,
                account_dir=self.provenance_executor.account_dir,
                did=envelope.from_,
                message=message.encode("utf-8"),
                signature=signature_bytes,
            )

            if not is_valid:
                self.logger.error(
                    "envelope.verify.signature_invalid",
                    envelope_from=envelope.from_,
                    epoch=envelope.epoch,
                )
            return is_valid
        except Exception as exc:
            self.logger.error(
                "envelope.verify.network_error",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                error=str(exc),
            )
            return False
