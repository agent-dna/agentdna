import asyncio
import json
import os
import copy
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from multiformats_cid.cid import CIDv0
import hashlib
import random
from .trust import RubixTrustService

import os

def ensure_agent_nft_artifact():
    """
    Ensures an NFT artifact file exists.
    Respects NFT_ARTIFACT_PATH or falls back to 'agent_nft_artifact'.
    Returns the full path.
    """
    file_path = os.getenv("NFT_ARTIFACT_PATH", "agent_nft_artifact")

    # Create file if missing
    if not os.path.exists(file_path):
        with open(file_path, "w") as f:
            f.write("")  # empty file
            

    return file_path


def ensure_agent_nft_metadata():
    """
    Ensures an NFT metadata file exists.
    Respects NFT_METADATA_PATH or falls back to 'agent_nft_metadata'.
    Returns the full path.
    """
    file_path = os.getenv("NFT_METADATA_PATH", "agent_nft_metadata")

    # Create file if missing
    if not os.path.exists(file_path):
        with open(file_path, "w") as f:
            f.write("")  # empty file

    return file_path

def _default_config_path() -> Path:
    # Same logic as NodeClient
    return Path(__file__).resolve().parent.parent / "config.json"

def check_if_agent_id_exists(agent_id: str, agent_info: List[dict]) -> bool:
    """
    Check if the given agent_id exists in the agent_info dictionary.
    """
    for agent in agent_info:
        if agent.get("agent_id", "") == agent_id:
            return True
    return False


def load_nft_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    if not config_path:
        config_path = _default_config_path()

    cfg_nft: Dict[str, Any] = {}
    try:
        with Path(config_path).open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg_nft = cfg.get("nft", {}) or {}
    except Exception:
        cfg_nft = {}

    default_value = 0.001
    default_data = "init data"

    return {
        "value": float(os.getenv("NFT_VALUE", cfg_nft.get("value", default_value))),
        "data": os.getenv("NFT_INIT_DATA", cfg_nft.get("data", default_data)),
        "password": cfg_nft.get("password"),
        "timeout": float(cfg_nft.get("timeout", 100.0)),
        "quorum_type": int(cfg_nft.get("quorum_type", 2)),
    }

def get_nft_data_for_deployment(agent_alias) -> str:
    if agent_alias == "":
        raise ValueError("agent_alias must be provided")

    nft_data = {
        "agent_name": agent_alias
    }
    return json.dumps(nft_data)

class RubixMessageHandler:
    """
    High-level Rubix helper for A2A agents.

    It exposes ONLY two main calls for developers:

      - build(...)
      - handle(...)

    and hides all Rubix / NFT details inside.

    Usage patterns
    --------------

    1) Host → Remote: build the signed host block, then embed into A2A message:

        dna = RubixMessageHandler(alias="host")

        built = dna.build(
            kind="host_request",
            original_message=task_text,
            state={"task_id": task_id, "context_id": context_id},  # optional
        )

        # built["host_json"] is what you put into the A2A text part
        host_json   = built["host_json"]
        message_id  = built["message_id"]
        context_id  = built["context_id"]

        # A2A payload (example)
        payload = {
            "message": {
                "role": "user",
                "parts": [
                    {"type": "text", "text": host_json},
                ],
                "messageId": message_id,
                "contextId": context_id,
            }
        }

    2) Remote agent (Karley / Nate / Kaitlyn) when receiving a message:

        dna = RubixMessageHandler(alias="karley", enable_nft=False)

        verify_info = await dna.handle(
            kind="incoming_request",
            raw_text=raw_from_context,
            verify_mode="light",   # or "heavy" in future
        )

        original_message = verify_info["original_message"]
        host_block       = verify_info["host_block"]
        host_ok          = verify_info["host_ok"]
        trust_issues     = verify_info["trust_issues"]

        # Use original_message for your local LLM/ADK agent

    3) Remote agent → Host: sign its response in a symmetric way:

        built_resp = dna.build(
            kind="agent_response",
            original_message=original_message,
            response=agent_result,
            host_block=host_block,
            extra={"host_trust_issues": trust_issues},
        )

        # Put built_resp["combined_json"] into your A2A text part

    4) Host handling remote responses (verify + optional NFT):

        result = await dna.handle(
            kind="host_response",
            resp_parts=resp_parts,      # list of {"text": "..."} from A2A artifacts
            original_task=task_text,
            remote_name=agent_name,
            execute_nft=True,           # will store in NFT if verification passes
        )

        messages     = result["messages"]      # verified messages w/ host+agent blocks
        trust_issues = result["trust_issues"]  # any issues
        nft_result   = result["nft_result"]    # optional: NFT execution response / None
    """

    def __init__(
        self,
        alias: str,
        api_key: str,
        token_filename: str = "agent_info.json",
        trust_service: Optional[RubixTrustService] = None,
        enable_nft: bool = True,
    ) -> None:
        # Trust layer (sign + verify + verify_message_payload)
        self.trust = trust_service or RubixTrustService(alias=alias, api_key=api_key)
        self.did = self.trust.did
        self.signer = self.trust.signer

        self.alias = alias

        # NFT config
        self.enable_nft = enable_nft
        self.nft_cfg = load_nft_config()
        self.last_parts: List[Dict[str, Any]] = []

        self.last_trust_issues: List[str] = []
        self.last_verification_status: str = "unknown"  # "ok" | "failed" | "unknown"
        self.inject_fake: bool = False


        # Where to store NFT token (host project, not site-packages)
        self.token_path: Optional[Path] = None
        self.nft_token: Optional[str] = None

        if self.enable_nft:
            env_path = os.getenv("AGENTDNA_TOKEN_PATH")
            if env_path:
                self.token_path = Path(env_path)
            else:
                home_dir = Path.home()
                token_dir = home_dir / ".agentdna"
                token_dir.mkdir(parents=True, exist_ok=True)
                self.token_path = token_dir / token_filename

            print("Path:", self.token_path)
            self.nft_token = self._load_or_deploy_nft()
            print("✅ Rubix NFT for alias", alias, ":", self.nft_token)
        else:
            print("ℹ️ RubixMessageHandler(enable_nft=False) – NFT operations disabled")

    # ======================================================================
    # Public API: BUILD
    # ======================================================================

    def build(
        self,
        *,
        kind: str,
        original_message: str,
        state: Optional[Dict[str, Any]] = None,
        response: Optional[str] = None,
        host_block: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Unified build() method.

        kind:
          - "host_request"   : host builds + signs a host envelope
          - "agent_response" : remote builds + signs its agent envelope and
                               combines it with the host block
        """
        kind = (kind or "").lower().strip()
        state = state or {}
        extra = extra or {}
        print(f"[RubixMessageHandler.build] kind={kind}")
        if kind == "host_request":
            # host → remote
            return self._build_host_request(
                original_message=original_message,
                state=state,
            )

        if kind == "agent_response":
            # remote → host
            if response is None:
                raise ValueError("build(kind='agent_response') requires `response`.")
            return self._build_agent_response(
                original_message=original_message,
                response=response,
                host_block=host_block,
                extra=extra,
            )

        raise ValueError(f"Unsupported build kind: {kind}")
    # ======================================================================
    # API: HANDLE
    # ======================================================================

    async def handle(
        self,
        *,
        kind: str,
        raw_text: Optional[str] = None,
        resp_parts: Optional[List[Dict[str, Any]]] = None,
        original_task: Optional[str] = None,
        remote_name: Optional[str] = None,
        verify_mode: str = "light",
        execute_nft: bool = True,
    ) -> Dict[str, Any]:
        kind = (kind or "").lower().strip()

        if kind == "remote":
            # remote agent handling inbound from host
            if raw_text is None:
                raise ValueError("handle(kind='remote') requires raw_text.")
            return self.trust.verify_message_payload(raw_text=raw_text, mode=verify_mode)

        if kind == "host":
            # host handling inbound from remote
            if resp_parts is None or original_task is None or remote_name is None:
                raise ValueError(
                    "handle(kind='host') requires resp_parts, original_task, remote_name."
                )
            return await self._handle_host_response(
                resp_parts=resp_parts,
                original_task=original_task,
                remote_name=remote_name,
                execute_nft=execute_nft,
            )

        raise ValueError(f"Unsupported handle kind: {kind}")

    # ======================================================================
    # Internal: host request building
    # ======================================================================

    def _build_host_request(
        self,
        original_message: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        task_id = state.get("task_id") or str(uuid.uuid4())
        context_id = state.get("context_id") or str(uuid.uuid4())
        message_id = str(uuid.uuid4())

        host_envelope = {
            "original_message": original_message,
            "task_id":          task_id,
            "context_id":       context_id,
            "message_id":       message_id,
            "timestamp":        datetime.utcnow().isoformat() + "Z",
        }

        host_block = self.trust.sign_envelope(host_envelope)

        host_json = json.dumps(
            {"host": host_block},
            separators=(",", ":"),
            sort_keys=True,
        )

        return {
            "kind":        "host_request",
            "host_block":  host_block,
            "host_json":   host_json,
            "task_id":     task_id,
            "context_id":  context_id,
            "message_id":  message_id,
        }

    # ======================================================================
    # Internal: agent response building
    # ======================================================================

    def _build_agent_response(
        self,
        original_message: str,
        response: str,
        host_block: Optional[Dict[str, Any]],
        extra: Dict[str, Any],
    ) -> Dict[str, Any]:
        envelope = {
            "original_message": original_message,
            "response":         response,
        }
        # Optionally attach trust metadata (e.g. host_trust_issues)
        envelope.update(extra)

        agent_block = self.trust.sign_envelope(envelope)

        combined: Dict[str, Any] = {
            "agent": agent_block,
        }
        if host_block is not None:
            combined["host"] = host_block

        combined_json = json.dumps(
            combined,
            separators=(",", ":"),
            sort_keys=True,
        )

        return {
            "kind":          "agent_response",
            "host_block":    host_block,
            "agent_block":   agent_block,
            "envelope":      envelope,
            "combined_json": combined_json,
        }

    # ======================================================================
    # Internal: NFT helpers
    # ======================================================================

    def execute_nft(self, nft_address: str, payload: Any) -> Dict[str, Any]:
        """
        Execute an NFT using the signer from the trust service.
        """
        nft_data = json.dumps(payload)

        try:
            print("NFT address:", nft_address)
            print("NFT data:", nft_data)
            response = self.signer.execute_nft(
                nft_address=nft_address,
                nft_data=nft_data,
            )
        except Exception as e:
            raise RuntimeError(f"Rubix execute_nft call failed: {e}")

        if not response.get("status", False):
            raise RuntimeError(
                f"NFT Execution Failed: {response.get('message', '<no message>')}"
            )

        return response

    def _load_or_deploy_nft(self) -> str:
        agent_id_structure = f"{self.signer.did}.{self.alias}" # Agent ID: DID + alias
        digest = hashlib.sha256(agent_id_structure.encode("utf-8")).digest()
        multihash_bytes = bytes([0x12, len(digest)]) + digest
        cid = CIDv0(multihash_bytes)
        agent_id = cid.encode().decode("utf-8")

        if not self.token_path:
            raise RuntimeError("Agent info path not initialized (enable_nft=False?)")

        if self.token_path.exists():
            f = open(self.token_path, "r", encoding="utf-8")
            try:
                agent_info = json.load(f)
                if check_if_agent_id_exists(agent_id, agent_info):
                    print("Using existing Agent ID from: ", self.token_path)
                    f.close()
                    return agent_id
                else:
                    f.close() # Close the file before redeploying
                    print(f"Agent ID not found in {self.token_path}, deploying new Agent")

                    resp = self.signer.deploy_nft(
                        nft_id=agent_id,        
                        nft_value=self.nft_cfg["value"] or 5,
                        nft_data=get_nft_data_for_deployment(agent_alias=self.alias),
                    )

                    if resp.get("error"):
                        raise RuntimeError(f"NFT deployment failed: {resp['error']}")

                    nft_address = resp["nft_address"]
                    if nft_address is None:
                        raise RuntimeError("unexpected error during Agent deployment: unable to fetch Agent ID")

                    agent_info.append({
                        "agent_id": nft_address, 
                        "agent_did": self.signer.did,
                        "agent_name": self.alias,
                    })

                    new_f = open(self.token_path, "w", encoding="utf-8")
                    try:
                        json.dump(agent_info, new_f, indent=2)
                        print("Updated Agent info in: ", self.token_path)
                    finally:
                        new_f.close()

                    return nft_address
            except Exception as e:
                raise RuntimeError(f"Failed to read agent info: {e}")
        else:
            print(f"agent_info.json not found at {self.token_path}, deploying new Agent")

            resp = self.signer.deploy_nft(
                nft_id=agent_id,        
                nft_value=self.nft_cfg["value"] or 5,
                nft_data=get_nft_data_for_deployment(agent_alias=self.alias),
            )
            if resp.get("error"):
                raise RuntimeError(f"NFT deployment failed: {resp['error']}")

            nft_address = resp["nft_address"]
            if nft_address is None:
                raise RuntimeError("unexpected error during Agent deployment: unable to fetch Agent ID")

            # Create agent_info.json file and store the nft_id
            agent_info = [
                {
                    "agent_id": nft_address, 
                    "agent_did": self.signer.did,
                    "agent_name": self.alias,
                }
            ]
            f = open(self.token_path, "w", encoding="utf-8")
            try:
                json.dump(agent_info, f, indent=2)
                print("Stored Agent info in: ", self.token_path)
            finally:
                f.close()

            return nft_address

    # ======================================================================
    # Internal: host handling remote responses
    # ======================================================================

    async def _handle_host_response(
        self,
        resp_parts: List[Dict[str, Any]],
        original_task: str,
        remote_name: str,
        execute_nft: bool,
    ) -> Dict[str, Any]:
        verified: List[Dict[str, Any]] = []
        trust_issues: List[str] = []
        error_msg: Optional[str] = None
        nft_result: Optional[Dict[str, Any]] = None

        for part in resp_parts:
            raw_text = part.get("text") or part.get("content", "")
            try:
                payload = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError):
                continue

            if not isinstance(payload, dict) or "agent" not in payload:
                continue

            host_block = payload.get("host")
            agent_block = payload["agent"]

            signer_did = agent_block.get("agent")
            env = agent_block.get("envelope", {})
            sig = agent_block.get("signature")

            if not (signer_did and env and sig and isinstance(env, dict)):
                trust_issues.append("Missing fields in agent block")
                print("Missing fields in agent block")
                continue

            env_verified = copy.deepcopy(env)

            if not self.trust.verify_envelope(signer_did, env_verified, sig):
                trust_issues.append(f"Invalid signature from {signer_did}")
                print(f"Invalid signature from {signer_did}")
                continue

            if env_verified.get("original_message") != original_task:
                trust_issues.append("Original message mismatch (before tamper)")
                print("Original message mismatch (before tamper)")

            agent_sig_valid = True
            env_to_store = copy.deepcopy(env_verified)

            if self.inject_fake:
                env_to_store["original_message"] = (
                    (env_to_store.get("original_message") or "") + " [TAMPERED]"
                )
                agent_sig_valid = False
                trust_issues.append(
                    "Tampering: original_message changed"
                )
                print("⚠️ Tampering with original message")

            verified.append(
                {
                    "host": host_block,
                    "agent": {
                        **agent_block,
                        "envelope": env_to_store,
                    },
                    "agent_sig_valid": agent_sig_valid,
                }
            )
            print("Verified agent block from", signer_did)

        if not verified and not trust_issues:
            error_msg = "No valid envelope response"
            print("No valid envelope response")

        # Save for NFT payload
        self.last_parts = verified
        self.last_trust_issues = trust_issues or []

        # Overall status: failed if any trust issue OR any invalid sig OR no verified
        if not verified:
            self.last_verification_status = "failed"
        else:
            all_valid = all(entry.get("agent_sig_valid", False) for entry in verified)
            if self.last_trust_issues or not all_valid:
                self.last_verification_status = "failed"
            else:
                self.last_verification_status = "ok"

        # ✅ Still write to NFT even if status == "failed" or inject_fake == True
        if (
            self.enable_nft
            and execute_nft
            and verified
            and self.nft_token is not None
        ):
            try:
                payload = self._build_nft_payload(remote_name)
                nft_result = await asyncio.to_thread(
                    self.execute_nft,
                    self.nft_token,
                    payload,
                )
                print("🚀 NFT execution result:", nft_result)
            except Exception as e:
                print("⚠️ NFT execution failed:", e)

        return {
            "messages": verified,
            "trust_issues": self.last_trust_issues or None,
            "error": error_msg,
            "nft_result": nft_result,
        }

    def _build_nft_payload(self, remote_name: str) -> Dict[str, Any]:
        """
        Build the JSON that will be stored in the NFT.
        Includes verification status + trust issues.
        """
        host_block = None
        responses: List[Dict[str, Any]] = []

        for entry in self.last_parts:
            if not host_block and entry.get("host"):
                host_block = entry["host"]
            if entry.get("agent"):
                # deep copy to avoid mutating internal state
                agent_entry = copy.deepcopy(entry["agent"])
                env = agent_entry.get("envelope", {}) or {}
                # overwrite / inject host_trust_issues with the FINAL host view
                env["host_trust_issues"] = self.last_trust_issues
                agent_entry["agent_did"] = agent_entry.get("agent")
                agent_entry["agent"] = remote_name
                agent_entry["envelope"] = env
                responses.append(agent_entry)

        return {
            "comment":  f"Agent communication initiation to {remote_name}",
            "executor": "host_agent",
            "did":      self.did,
            "verification": {
                "status": self.last_verification_status,   # "ok" | "failed"
                "trust_issues": self.last_trust_issues,    # list of strings
                "inject_fake": self.inject_fake,           # whether tamper sim was ON
            },
            "host":     host_block,
            "responses": responses,
        }