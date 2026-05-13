import json
import os
from typing import Any, Dict, Optional, List
from pathlib import Path
import requests
from rubix.client import RubixClient
from rubix.signer import Signer
from rubix.did import online_signature_verify, signatureResponseError

from .node_client import resolve_chain_url


class RubixTrustService:
    def __init__(
        self,
        alias: str,
        api_key: str,
        config_path: str = "",
        timeout: float = 300.0,
        chain_url: Optional[str] = None,
        node_config_path: Optional[str] = None,
    ) -> None:
        if api_key == "":
            raise ValueError("API Key needs to be provided. Visit https://agentdna.io/ and join the" \
            "Beta programme to get an API Key.")

        self.base_url = resolve_chain_url(
            chain_url=chain_url,
            config_path=node_config_path,
        )
        self.timeout = timeout

        if config_path == "":
            home_dir = Path.home()
            config_dir = os.path.join(home_dir, ".agentdna")
        else:
            config_dir = config_path

        client = RubixClient(node_url=self.base_url, timeout=timeout, api_key=api_key)
        self.signer = Signer(rubixClient=client, alias=alias, config_path=config_dir)
        self.did = self.signer.did

        print("✅ RubixTrustService DID:", self.did)
        print("✅ RubixTrustService base URL:", self.base_url)

    # ---------- signing ----------

    def sign_envelope(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sign an envelope dict and return a block:
          { "agent": <did>, "envelope": {...}, "signature": "<hex>" }
        """
        print("Agent is going to sign")
        print("Public Key (hex): ", self.signer.get_keypair().public_key)
        keypair = self.signer.get_keypair()

        envelope_json = json.dumps(envelope, sort_keys=True)
        envelope_bytes = envelope_json.encode("utf-8")
        signature_bytes = keypair.sign(envelope_bytes)
        signature_hex = signature_bytes.hex()
        return {
            "agent": self.did,
            "envelope": envelope,
            "signature": signature_hex,
        }

    # ---------- verification ----------

    def verify_envelope(
        self,
        signer_did: str,
        envelope: Dict[str, Any],
        signature: str,
        timeout: Optional[float] = None,
    ) -> bool:
        envelope_json = json.dumps(envelope, sort_keys=True)
        message_bytes = envelope_json.encode("utf-8")

        try:
            signature_bytes = bytes.fromhex(signature)
        except ValueError:
            print("⚠️ verify_envelope: invalid hex signature string")
            return False

        try:
            is_valid = online_signature_verify(
                rubixNodeBaseUrl=self.base_url,
                did=signer_did,
                message=message_bytes,
                signature=signature_bytes,
            )
            return bool(is_valid)
        except signatureResponseError as e:
            print(f"⚠️ verify_envelope error: {e}")
            return False

    def verify_message_payload(
        self,
        raw_text: str,
        mode: str = "light",
    ) -> Dict[str, Any]:
        effective_mode = (mode or os.getenv("AGENTDNA_VERIFY_MODE") or "light").lower()

        result: Dict[str, Any] = {
            "original_message": raw_text,
            "host_block": None,
            "host_ok": None,
            "trust_issues": [],
            "agent_checks": [],
            "verified": False,      # final overall flag
        }

        if not raw_text:
            return result

        try:
            payload = json.loads(raw_text)
        except Exception:
            # plain text: nothing to verify
            return result

        if not isinstance(payload, dict):
            return result

        host_block = None

        if "host" in payload and isinstance(payload["host"], dict):
            host_block = payload["host"]
        elif all(k in payload for k in ("agent", "envelope", "signature")):
            host_block = payload

        result["host_block"] = host_block

        env = None
        if isinstance(host_block, dict):
            env = host_block.get("envelope", {})
            if isinstance(env, dict):
                orig = env.get("original_message")
                if isinstance(orig, str):
                    result["original_message"] = orig

        # Start optimistic and flip to False when something fails
        overall_ok = True

        # ---- Host verification (always done) ----
        if isinstance(host_block, dict) and isinstance(env, dict):
            signer_did = host_block.get("agent")
            sig = host_block.get("signature")

            if signer_did and sig:
                ok = self.verify_envelope(signer_did, env, sig)
                result["host_ok"] = bool(ok)
                if not ok:
                    overall_ok = False
                    result["trust_issues"].append(
                        f"Invalid host signature for DID {signer_did}"
                    )
            else:
                overall_ok = False
                result["trust_issues"].append(
                    "Host block missing agent/envelope/signature"
                )
        else:
            overall_ok = False
            result["trust_issues"].append("No host block found in payload")

        # ---- Agent verification (heavy mode) ----
        if effective_mode == "heavy":
            agent_blocks: List[Dict[str, Any]] = []

            if "agent" in payload and isinstance(payload["agent"], dict):
                agent_blocks.append(payload["agent"])

            responses = payload.get("responses")
            if isinstance(responses, list):
                for r in responses:
                    if isinstance(r, dict):
                        agent_blocks.append(r)

            for ab in agent_blocks:
                a_env = ab.get("envelope", {})
                a_sig = ab.get("signature")
                a_did = ab.get("agent")

                if not (a_did and a_sig and isinstance(a_env, dict)):
                    overall_ok = False
                    result["agent_checks"].append(
                        {
                            "agent": a_did or "<unknown>",
                            "ok": False,
                            "envelope": a_env if isinstance(a_env, dict) else {},
                            "reason": "Agent block missing agent/envelope/signature",
                        }
                    )
                    result["trust_issues"].append(
                        "Agent block missing agent/envelope/signature"
                    )
                    continue

                ok = self.verify_envelope(a_did, a_env, a_sig)
                result["agent_checks"].append(
                    {
                        "agent": a_did,
                        "ok": bool(ok),
                        "envelope": a_env,
                        "reason": None if ok else "Agent signature invalid",
                    }
                )
                if not ok:
                    overall_ok = False
                    result["trust_issues"].append(
                        f"Invalid signature from agent {a_did}"
                    )

        result["verified"] = overall_ok
        return result