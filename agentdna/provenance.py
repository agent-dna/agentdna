import os
import json

from typing import Any
from rubix.client import RubixClient
from rubix.signer import Signer
from rubix.querier import Querier
from rubix.did import online_signature_verify

from .types import Envelope
from .helpers import canonicalize_envelope
from .id import get_agent_card_id
from .config import get_default_config_dir

class Provenance:
    def __init__(
        self,
        name: str,
        provenance_url: str = "https://chain-connector-2.rubix.net",
        api_key: str = "",
        config_path: str = "",
        timeout_in_seconds: int = 10000
    ):
        if name == "":
            raise NameError("'name' attribute cannot be empty")

        if provenance_url == "":
            raise ValueError("'provenance_url' attribute cannot be empty")
        
        self.provenance_url = provenance_url

        provenance_client = RubixClient(
            node_url=provenance_url,
            timeout=timeout_in_seconds,
            api_key=api_key
        )

        self.config_dir = (
            config_path
            if config_path
            else get_default_config_dir()
        )
        os.makedirs(
            self.config_dir,
            exist_ok=True,
        )

        self.provenance_executor = Signer(
            rubixClient=provenance_client,
            alias=name,
            config_path=self.config_dir
        )

        self.provenance_querier = Querier(
            rubixClient=provenance_client,
        )
        
        self.provenance_id = self.provenance_executor.did

    def create_new_provenance_card(
        self, 
        card_id: str, 
        card_info: str,
        card_value: float = 0.001,
    ):
        return self.provenance_executor.deploy_nft(
            nft_id=card_id,
            nft_value=card_value,
            nft_data=card_info
        )

    def create_new_child_provenance_card(self, parent_card_id: str, card_info: str):
        response = self.provenance_executor.create_child_nft(
            parent_nft_address=parent_card_id,
            nft_data=card_info,
        )

        if response.get("error") is not None:
            raise Exception(f"Child NFT creation failed: {response['error']}")
        else:
            if len(response["child_nfts"]) == 0:
                raise Exception(f"Child Provenance Card creation failed for parent card {parent_card_id}, no card was created")
            
            child_provenance_card_id = response["child_nfts"][0].get("childNFTId")
            if child_provenance_card_id is None:
                raise Exception(
                    f"Child Provenance Card creation failed for parent card "
                    f"{parent_card_id}, unable to extract id from "
                    f"{response['child_nfts']}"
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
        return self.provenance_executor.execute_nft(
            nft_address=card_id, 
            nft_data=card_info,
        )

    def provenance_card_history(self, actor_id: str):
        """
        List the set of records written on a Card
        """
        if actor_id == "":
            raise ValueError("agent_id cannot be empty")
        
        if not actor_id.startswith("bafy"):
            raise ValueError(f"invalid agent_id provided: {actor_id}, it should start with 'bafy'")

        agent_card_id = get_agent_card_id(agent_id=actor_id)
        
        return self.provenance_querier.get_nft_states(
            nft_address=agent_card_id
        )

    def get_latest_provenance_record(
        self,
        actor_id: str,
    ) -> dict[str, Any]:
        """
        Returns the latest provenance record
        associated with the supplied actor.
        """
        history = self.provenance_card_history(
            actor_id=actor_id
        )

        if len(history) == 0:
            raise ValueError(
                f"no provenance records found "
                f"for actor {actor_id}"
            )

        latest_record = history[-1]

        data = latest_record.get("data")

        if data is None:
            raise ValueError(
                "latest provenance record "
                "does not contain data"
            )

        return json.loads(data)
    
    def sign_envelope(
        self,
        envelope: Envelope,
    ) -> str:
        """
        Signs an envelope and returns
        a hex encoded signature.
        """

        keypair = self.provenance_executor.get_keypair()

        message = canonicalize_envelope(
            envelope
        )

        signature_bytes = keypair.sign(
            message.encode("utf-8")
        )

        return signature_bytes.hex()
    
    def verify_envelope(
        self,
        envelope: Envelope,
    ) -> bool:
        """
        Verifies an envelope signature
        against the supplied actor DID.
        """

        try:
            signature_bytes = bytes.fromhex(
                envelope.signature
            )

        except ValueError:
            return False

        message = canonicalize_envelope(
            envelope
        )

        try:
            is_valid = online_signature_verify(
                rubixNodeBaseUrl=self.provenance_url,
                did=envelope.from_.id,
                message=message.encode("utf-8"),
                signature=signature_bytes,
            )

            return bool(is_valid)
        except Exception:
            return False