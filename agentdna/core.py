from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from multiformats_cid.cid import CIDv0

from .trust import RubixTrustService


# ──────────────────────────────────────────────────────────────────────────────
# NFT config loader (file + env, with sensible defaults)
# ──────────────────────────────────────────────────────────────────────────────

def _default_nft_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.json"


def _deep_json_decode(obj: Any) -> Any:
    """
    Recursively replace JSON-string values with their parsed equivalents.

    Used by ``AgentDNA.history()`` so chain records render as a foldable tree
    end-to-end. Strings that aren't valid JSON (or don't open with ``{`` /
    ``[``) are returned untouched, so plain free-text fields stay strings.
    """
    if isinstance(obj, dict):
        return {k: _deep_json_decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_json_decode(item) for item in obj]
    if isinstance(obj, str):
        stripped = obj.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return _deep_json_decode(json.loads(obj))
            except (TypeError, json.JSONDecodeError):
                pass
    return obj


def _load_nft_config(config_path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    if not config_path:
        config_path = _default_nft_config_path()

    cfg_nft: Dict[str, Any] = {}
    try:
        with Path(config_path).open("r", encoding="utf-8") as f:
            cfg_nft = (json.load(f).get("nft") or {})
    except Exception:
        cfg_nft = {}

    return {
        "value":       float(os.getenv("NFT_VALUE", cfg_nft.get("value", 0.001))),
        "data":        os.getenv("NFT_INIT_DATA", cfg_nft.get("data", "init data")),
        "password":    cfg_nft.get("password"),
        "timeout":     float(cfg_nft.get("timeout", 100.0)),
        "quorum_type": int(cfg_nft.get("quorum_type", 2)),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Public types: SignedEnvelope (wire-ready string + metadata) and VerifyResult
# (typed verify outcome).  Both are additive — they don't replace the dicts
# returned by the legacy build()/handle() entry points.
# ──────────────────────────────────────────────────────────────────────────────

class SignedEnvelope(str):
    """
    Wire-ready signed host envelope.

    Behaves as a string (the JSON you put on transport) and carries the
    underlying signed block as attributes::

        env = dna.envelope({"tool": "append_task", "args": {...}})
        send_over_wire(env)             # env IS the wire string
        env.host_block                  # the signed dict
        env.message_id, env.context_id  # ids
        env.original_message            # the JSON we signed
    """

    _ATTRS = ("host_block", "message_id", "context_id", "original_message", "user_block")

    def __new__(cls, wire: str, **attrs):
        obj = super().__new__(cls, wire or "")
        for name in cls._ATTRS:
            object.__setattr__(obj, name, attrs.get(name))
        return obj


@dataclass
class VerifyResult:
    """
    Typed outcome of verifying a signed reply (host side).

    Read this instead of walking the dict returned by the legacy handle() path.
    """

    payload: Optional[Any] = None                  # parsed reply body (JSON or str)
    verified: bool = False                          # all signature checks passed
    trust_issues: List[str] = field(default_factory=list)
    signed_text: Optional[str] = None               # the inner combined_json we verified
    host_block: Optional[Dict[str, Any]] = None
    agent_block: Optional[Dict[str, Any]] = None
    nft_result: Optional[Dict[str, Any]] = None
    verification_status: str = "unknown"            # "ok" | "failed" | "unknown"
    user_block: Optional[Dict[str, Any]] = None     # signed user envelope (delegation chain)
    user_verified: bool = False                     # was the user's signature valid?


@dataclass
class RequestContext:
    """
    Verified inbound request (server side).

    A server tool calls ``dna.verify_request(envelope)`` to get one of these,
    then passes it to ``dna.sign_response(payload, ctx=ctx)`` so the reply is
    cryptographically stitched to the exact request it answers.
    """

    original_message: str = ""                      # the JSON string the host signed
    host_block: Optional[Dict[str, Any]] = None     # signed host block (passed back when replying)
    trust_issues: List[str] = field(default_factory=list)
    verified: bool = False
    user_block: Optional[Dict[str, Any]] = None     # signed user envelope (delegation chain)
    user_id: Optional[str] = None                   # user DID from the signed user envelope
    user_intent: Optional[str] = None               # the intent string the user signed
    user_verified: bool = False                     # was the user's signature valid?


# ──────────────────────────────────────────────────────────────────────────────
# AgentDNA — single entry point. Owns envelope construction, response
# verification, and the NFT audit log. Sign/verify primitives live in
# RubixTrustService (the rubix-py SDK adapter).
# ──────────────────────────────────────────────────────────────────────────────

class AgentDNA:
    """
    Single entry point for agent developers.

    Two ergonomic helpers (recommended):

        env    = dna.envelope(payload)              # sign outbound
        result = await dna.verify_reply(            # verify inbound + NFT
            raw_text, original=payload,
        )

    Plus low-level primitives if you need them:

        dna.build(original_message=...)             # returns dict
        await dna.handle(raw_text=...)              # returns dict
        await dna.handle(resp_parts=..., original_task=..., remote_name=...)

    And one-line conveniences:

        AgentDNA.from_env(alias="...")              # build from env vars
        dna.history()                               # decoded chain history
    """

    # ─── Construction ────────────────────────────────────────────────────────

    def __init__(
        self,
        alias: str,
        api_key: str,
        chain_url: Optional[str] = None,
        token_filename: str = "agent_info.json",
        enable_nft: bool = True,
        **_legacy,
    ) -> None:
        # Back-compat: silently accept role= from older callers
        _legacy.pop("role", None)

        # Trust layer — sign/verify primitives via rubix-py
        self.trust = RubixTrustService(alias=alias, api_key=api_key, chain_url=chain_url)
        self.did = self.trust.did
        self.signer = self.trust.signer

        self.alias = alias
        self.token_filename = token_filename

        # NFT config + per-call audit state
        self.enable_nft = enable_nft
        self.nft_cfg = _load_nft_config()
        self.last_parts: List[Dict[str, Any]] = []
        self.last_trust_issues: List[str] = []
        self.last_verification_status: str = "unknown"  # "ok" | "failed" | "unknown"

        # NFT registration — populated eagerly below.
        self.token_path: Optional[Path] = None
        self.nft_token: Optional[str] = None

        # An agent's audit-log NFT is bound to its DID. Deploy it eagerly so the
        # chain identity exists the moment the agent is constructed (cached in
        # agent_info.json on first deploy; subsequent constructions reuse it).
        # Pass enable_nft=False at construction to skip — useful for pure-remote
        # agents that never write to chain.
        if self.enable_nft:
            self._ensure_nft_token()

    @classmethod
    def from_env(cls, alias: Optional[str] = None, **overrides) -> "AgentDNA":
        """
        Construct AgentDNA from conventional env vars:

            AGENTDNA_API_KEY  (required)
            AGENTDNA_ALIAS    (used if `alias=` not provided)
            CHAIN_URL         (optional)

        Any kwarg passed to from_env() overrides the env-var of the same name.
        """
        api_key = overrides.pop("api_key", None) or os.environ.get("AGENTDNA_API_KEY")
        if not api_key:
            raise RuntimeError("AGENTDNA_API_KEY not set (and api_key= not passed)")

        final_alias = alias or overrides.pop("alias", None) or os.environ.get("AGENTDNA_ALIAS")
        if not final_alias:
            raise RuntimeError("alias not provided (pass alias= or set AGENTDNA_ALIAS)")

        return cls(
            alias=final_alias,
            api_key=api_key,
            chain_url=overrides.pop("chain_url", None) or os.environ.get("CHAIN_URL"),
            **overrides,
        )

    # Back-compat: examples that reach into `dna.handler.xxx` keep working.
    @property
    def handler(self) -> "AgentDNA":
        return self

    # ─── New ergonomic API: envelope() / verify_reply() / history() ──────────

    # ─── Internal verify helpers (shared by handle() and aliases) ────────────

    async def _verify_reply(
        self,
        raw_text: Union[str, dict, list],
        *,
        original: Union[str, dict, list, SignedEnvelope],
        remote_name: Optional[str] = None,
        execute_nft: bool = True,
    ) -> VerifyResult:
        # Resolve `original` back to the exact string the host signed
        if isinstance(original, SignedEnvelope):
            original_str = original.original_message or str(original)
        elif isinstance(original, str):
            original_str = original
        else:
            original_str = json.dumps(original, sort_keys=True, ensure_ascii=False)

        signed_text = self._unwrap_signed_text(raw_text)
        if not signed_text:
            return VerifyResult(
                payload=None,
                verified=False,
                trust_issues=["empty or unparseable reply"],
                signed_text=None,
                verification_status="failed",
            )

        if remote_name is None:
            remote_name = self._infer_remote_name(signed_text) or "remote"

        raw_result = await self._handle_host_response(
            resp_parts=[{"text": signed_text}],
            original_task=original_str,
            remote_name=remote_name,
            execute_nft=execute_nft,
        )

        messages = raw_result.get("messages") or []
        first = messages[0] if messages else {}
        agent_block = first.get("agent") or {}
        host_block = first.get("host")
        env = agent_block.get("envelope") or {}

        # Extract the reply body. The server-signed envelope's "response" field
        # is what application code actually wants.
        body = env.get("response")
        parsed_payload: Any = body
        if isinstance(body, str):
            try:
                parsed_payload = json.loads(body)
            except (TypeError, json.JSONDecodeError):
                parsed_payload = body  # leave as plain string

        # Pull user_block + verify it (if a delegation chain was used)
        user_block = None
        user_verified = False
        if isinstance(host_block, dict):
            host_env = host_block.get("envelope") or {}
            if isinstance(host_env, dict) and isinstance(host_env.get("user_block"), dict):
                user_block = host_env["user_block"]
                user_did = user_block.get("agent")
                user_env = user_block.get("envelope") or {}
                user_sig = user_block.get("signature")
                if user_did and user_sig and isinstance(user_env, dict):
                    try:
                        user_verified = bool(
                            self.trust.verify_envelope(user_did, user_env, user_sig)
                        )
                    except Exception:
                        user_verified = False

        return VerifyResult(
            payload=parsed_payload,
            verified=(self.last_verification_status == "ok"),
            trust_issues=list(raw_result.get("trust_issues") or []),
            signed_text=signed_text,
            host_block=host_block,
            agent_block=agent_block,
            nft_result=raw_result.get("nft_result"),
            verification_status=self.last_verification_status,
            user_block=user_block,
            user_verified=user_verified,
        )

    async def _verify_request(
        self,
        envelope: Union[str, dict, None],
        *,
        verify_mode: str = "light",
    ) -> RequestContext:
        if envelope is None:
            return RequestContext(trust_issues=["No envelope provided"], verified=False)
        raw_text = envelope if isinstance(envelope, str) else json.dumps(envelope)
        info = self.trust.verify_message_payload(raw_text=raw_text, mode=verify_mode)

        host_block = info.get("host_block") or {}
        host_envelope = host_block.get("envelope") if isinstance(host_block, dict) else None
        user_block = host_envelope.get("user_block") if isinstance(host_envelope, dict) else None

        # Optionally verify the embedded user signature.
        user_id: Optional[str] = None
        user_intent: Optional[str] = None
        user_verified = False
        trust_issues = list(info.get("trust_issues") or [])
        if isinstance(user_block, dict):
            user_id = user_block.get("agent")
            user_env = user_block.get("envelope") or {}
            user_sig = user_block.get("signature")
            if user_id and user_sig and isinstance(user_env, dict):
                try:
                    user_verified = bool(
                        self.trust.verify_envelope(user_id, user_env, user_sig)
                    )
                except Exception:
                    user_verified = False
            if not user_verified:
                trust_issues.append(f"Invalid user signature for DID {user_id}")

            raw_intent = user_env.get("original_message")
            if isinstance(raw_intent, str):
                try:
                    parsed = json.loads(raw_intent)
                    user_intent = (
                        parsed.get("intent")
                        if isinstance(parsed, dict) and "intent" in parsed
                        else raw_intent
                    )
                except Exception:
                    user_intent = raw_intent

        return RequestContext(
            original_message=info.get("original_message") or "",
            host_block=info.get("host_block"),
            trust_issues=trust_issues,
            verified=bool(info.get("verified")) and (user_verified or user_block is None),
            user_block=user_block,
            user_id=user_id,
            user_intent=user_intent,
            user_verified=user_verified,
        )

    # ─── Optional explicit-name aliases (build/handle still the canonical) ───
    # Adopters who prefer self-documenting names can use these. They delegate
    # to the same dispatch as build()/handle().

    def envelope(self, payload, *, state=None):
        return self.build(payload, state=state)

    async def verify_reply(self, raw_text, *, original, remote_name=None, execute_nft=True):
        return await self.handle(
            raw_text,
            original=original,
            remote_name=remote_name,
            execute_nft=execute_nft,
        )

    async def verify_request(self, envelope, *, verify_mode="light"):
        return await self.handle(envelope, verify_mode=verify_mode)

    def sign_response(self, payload, *, ctx, extra=None):
        return self.build(payload, ctx=ctx, extra=extra)

    def history(self, latest: bool = False) -> List[Dict[str, Any]]:
        """
        Fetch decoded NFT chain history for this agent.

        Returns ``[]`` if the audit-log NFT hasn't been deployed yet (i.e. this
        agent has never verified a reply).

        Any field whose value is a JSON-encoded string (``NFTData``, ``data``,
        and nested ones like ``original_message``) is recursively parsed so
        renderers like ``st.json()`` get a foldable tree all the way down
        instead of one long escaped blob.
        """
        if not self.nft_token:
            return []

        # Local imports keep the chain-query dependency optional at import time.
        from rubix.client import RubixClient
        from rubix.querier import Querier

        client = RubixClient(node_url=self.trust.base_url, timeout=300)
        states = Querier(client).get_nft_states(
            nft_address=self.nft_token,
            only_latest_state=latest,
        )

        if isinstance(states, dict):
            states = [states]
        elif not isinstance(states, list):
            return []

        return [_deep_json_decode(s) for s in states]

    # ─── BUILD: outbound messages (legacy primitives, still supported) ───────

    def build(
        self,
        payload: Optional[Union[str, dict, list]] = None,
        *,
        ctx: Optional[RequestContext] = None,
        user: Optional[Union[SignedEnvelope, Dict[str, Any]]] = None,
        state: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        **legacy,
    ) -> Union[SignedEnvelope, str, Dict[str, Any]]:
        """
        Sign an outbound envelope. Two ergonomic call shapes plus the legacy
        kwarg form — all three coexist; pick whichever reads best.

        Sign a fresh request (host side) — positional payload, no ctx::

            env = dna.build(host_msg)              # returns SignedEnvelope (str-like)
            transport.send(str(env))

        Sign a reply under a verified context (remote side) — pass ``ctx=``::

            wire = dna.build(payload, ctx=ctx)     # returns wire string

        Pass ``user=`` to attach a user's signed intent (delegation chain).
        The host signature commits to the embedded ``user_block``, so any
        tampering with user attribution breaks host verification::

            user_signed = user_dna.build({"intent": prompt})  # user signs first
            env = dna.build(host_msg, user=user_signed)        # host signs over it

        Legacy kwarg form — same as before the refactor, returns a dict::

            built = dna.build(original_message=task)
            built = dna.build(original_message=task, response=reply,
                              host_block=host_block, extra={...})
        """
        # Legacy path — caller passed original_message= as a kwarg
        if "original_message" in legacy:
            original_message = legacy.pop("original_message")
            response = legacy.pop("response", None)
            host_block = legacy.pop("host_block", None)
            legacy_extra = legacy.pop("extra", None) or extra
            legacy_state = legacy.pop("state", None) or state
            if response is not None:
                return self._build_agent_response(
                    original_message=original_message,
                    response=response,
                    host_block=host_block,
                    extra=legacy_extra,
                )
            return self._build_host_request(
                original_message=original_message,
                state=legacy_state or {},
                user_block=self._extract_user_block(user) if user is not None else None,
            )

        if payload is None:
            raise ValueError(
                "build() needs a payload — pass it positionally or use "
                "the legacy original_message= kwarg."
            )

        # New ergonomic path
        if ctx is not None:
            # Sign a reply under a verified request context
            if isinstance(payload, str):
                response_str = payload
            else:
                response_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            merged_extra = {"host_trust_issues": ctx.trust_issues, **(extra or {})}
            built = self._build_agent_response(
                original_message=ctx.original_message or response_str,
                response=response_str,
                host_block=ctx.host_block,
                extra=merged_extra,
            )
            return built["combined_json"]

        # Sign a fresh request — returns SignedEnvelope
        if isinstance(payload, str):
            original_message = payload
        else:
            original_message = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        user_block = self._extract_user_block(user) if user is not None else None
        built = self._build_host_request(
            original_message=original_message,
            state=state or {},
            user_block=user_block,
        )
        return SignedEnvelope(
            built["host_json"],
            host_block=built["host_block"],
            message_id=built["message_id"],
            context_id=built["context_id"],
            original_message=original_message,
            user_block=user_block,
        )

    @staticmethod
    def _extract_user_block(user: Union[SignedEnvelope, Dict[str, Any], None]) -> Optional[Dict[str, Any]]:
        """
        Normalize ``user`` into a signed-block dict (``{agent, envelope, signature}``).
        Accepts a ``SignedEnvelope`` (from ``user_dna.build(...)``), a raw signed-block
        dict, or ``None``.
        """
        if user is None:
            return None
        if isinstance(user, SignedEnvelope):
            return user.host_block
        if isinstance(user, dict):
            if all(k in user for k in ("agent", "envelope", "signature")):
                return user
            raise ValueError("user dict must contain agent / envelope / signature keys")
        raise TypeError(
            f"user must be a SignedEnvelope or signed-block dict; got {type(user).__name__}"
        )

    def _build_host_request(
        self,
        *,
        original_message: str,
        state: Optional[Dict[str, Any]] = None,
        user_block: Optional[Dict[str, Any]] = None,
        **_extra,
    ) -> Dict[str, Any]:
        state = state or {}
        task_id = state.get("task_id") or str(uuid.uuid4())
        context_id = state.get("context_id") or str(uuid.uuid4())
        message_id = str(uuid.uuid4())

        host_envelope: Dict[str, Any] = {
            "original_message": original_message,
            "task_id":          task_id,
            "context_id":       context_id,
            "message_id":       message_id,
            "timestamp":        datetime.utcnow().isoformat() + "Z",
        }
        if user_block is not None:
            # Commit to the user attribution by signing it inside the host envelope.
            host_envelope["user_block"] = user_block

        host_block = self.trust.sign_envelope(host_envelope)
        host_json = json.dumps({"host": host_block}, separators=(",", ":"), sort_keys=True)

        return {
            "kind":       "host_request",
            "host_block": host_block,
            "host_json":  host_json,
            "task_id":    task_id,
            "context_id": context_id,
            "message_id": message_id,
            "user_block": user_block,
        }

    def _build_agent_response(
        self,
        *,
        original_message: str,
        response: str,
        host_block: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        **_extra,
    ) -> Dict[str, Any]:
        envelope: Dict[str, Any] = {"original_message": original_message, "response": response}
        if extra:
            envelope.update(extra)
        agent_block = self.trust.sign_envelope(envelope)

        combined: Dict[str, Any] = {"agent": agent_block}
        if host_block is not None:
            combined["host"] = host_block
        combined_json = json.dumps(combined, separators=(",", ":"), sort_keys=True)

        return {
            "kind":          "agent_response",
            "host_block":    host_block,
            "agent_block":   agent_block,
            "envelope":      envelope,
            "combined_json": combined_json,
        }

    # ─── HANDLE: inbound messages (legacy primitives, still supported) ───────

    async def handle(
        self,
        payload: Any = None,
        *,
        original: Optional[Union[str, dict, list, SignedEnvelope]] = None,
        remote_name: Optional[str] = None,
        verify_mode: str = "light",
        execute_nft: bool = True,
        **legacy,
    ) -> Union["VerifyResult", "RequestContext", Dict[str, Any]]:
        """
        Verify an inbound envelope. Two ergonomic call shapes plus the legacy
        kwarg form.

        Verify a signed reply (host side) — pass ``original=`` to compare
        against what we sent. Returns ``VerifyResult``, writes the audit-log
        NFT (set ``execute_nft=False`` to skip)::

            result = await dna.handle(reply_text, original=env)
            result.payload, result.verified, result.trust_issues

        Verify an inbound request (remote side) — returns ``RequestContext``::

            ctx = await dna.handle(dna_envelope)
            ctx.original_message, ctx.host_block, ctx.verified

        Legacy kwarg form (returns a dict)::

            info   = await dna.handle(raw_text=...)
            result = await dna.handle(
                resp_parts=...,
                original_task=...,
                remote_name=...,
            )
        """
        # Legacy: kwarg-based call
        if "raw_text" in legacy:
            return self.trust.verify_message_payload(
                raw_text=legacy["raw_text"],
                mode=legacy.get("verify_mode", verify_mode),
            )
        if "resp_parts" in legacy:
            resp_parts = legacy["resp_parts"]
            original_task = legacy.get("original_task")
            rname = legacy.get("remote_name", remote_name)
            for label, value in (("original_task", original_task), ("remote_name", rname)):
                if value is None:
                    raise ValueError(f"handle() requires {label}")
            return await self._handle_host_response(
                resp_parts=resp_parts,
                original_task=original_task,
                remote_name=rname,
                execute_nft=legacy.get("execute_nft", execute_nft),
            )

        # New ergonomic path
        if original is not None:
            # Verify a signed reply against what we sent
            return await self._verify_reply(
                payload,
                original=original,
                remote_name=remote_name,
                execute_nft=execute_nft,
            )

        if payload is None:
            raise ValueError(
                "handle() needs a payload — pass the envelope (positional) "
                "or use the legacy raw_text=/resp_parts= kwargs."
            )

        # Verify an inbound request → RequestContext
        return await self._verify_request(payload, verify_mode=verify_mode)

    async def _handle_host_response(
        self,
        *,
        resp_parts: List[Dict[str, Any]],
        original_task: str,
        remote_name: str,
        execute_nft: bool = True,
        **_extra,
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
                trust_issues.append("Original message mismatch")
                print("Original message mismatch")

            verified.append({
                "host": host_block,
                "agent": {**agent_block, "envelope": copy.deepcopy(env_verified)},
                "agent_sig_valid": True,
            })
            print("Verified agent block from", signer_did)

        if not verified and not trust_issues:
            error_msg = "No valid envelope response"
            print("No valid envelope response")

        # Snapshot for NFT payload + status
        self.last_parts = verified
        self.last_trust_issues = trust_issues or []
        if not verified:
            self.last_verification_status = "failed"
        else:
            self.last_verification_status = (
                "failed" if self.last_trust_issues else "ok"
            )

        # NFT write — lazy-deploy on first need
        if self.enable_nft and execute_nft and verified:
            token = self._ensure_nft_token()
            if token is not None:
                try:
                    nft_payload = self._build_nft_payload(remote_name)
                    nft_result = await asyncio.to_thread(self._execute_nft, token, nft_payload)
                    print("🚀 NFT execution result:", nft_result)
                except Exception as e:
                    print("⚠️ NFT execution failed:", e)

        return {
            "messages":     verified,
            "trust_issues": self.last_trust_issues or None,
            "error":        error_msg,
            "nft_result":   nft_result,
        }

    # ─── NFT: lazy deploy + execute ───────────────────────────────────────────

    def _ensure_nft_token(self) -> Optional[str]:
        """Deploy (or load) the audit-log NFT on first need. Cached after that."""
        if not self.enable_nft:
            return None
        if self.nft_token is not None:
            return self.nft_token

        if self.token_path is None:
            env_path = os.getenv("AGENTDNA_TOKEN_PATH")
            if env_path:
                self.token_path = Path(env_path)
            else:
                token_dir = Path.home() / ".agentdna"
                token_dir.mkdir(parents=True, exist_ok=True)
                self.token_path = token_dir / self.token_filename
            print("Path:", self.token_path)

        self.nft_token = self._load_or_deploy_nft()
        print("✅ Rubix NFT for alias", self.alias, ":", self.nft_token)
        return self.nft_token

    def _load_or_deploy_nft(self) -> str:
        # Agent ID = CIDv0(sha256(did.alias))
        digest = hashlib.sha256(f"{self.signer.did}.{self.alias}".encode("utf-8")).digest()
        multihash_bytes = bytes([0x12, len(digest)]) + digest
        agent_id = CIDv0(multihash_bytes).encode().decode("utf-8")

        if not self.token_path:
            raise RuntimeError("Agent info path not initialized")

        agent_info: List[Dict[str, Any]] = []
        if self.token_path.exists():
            try:
                with open(self.token_path, "r", encoding="utf-8") as f:
                    agent_info = json.load(f)
            except Exception as e:
                raise RuntimeError(f"Failed to read agent info: {e}")
            for agent in agent_info:
                if agent.get("agent_id") == agent_id:
                    print("Using existing Agent ID from:", self.token_path)
                    return agent_id
            print(f"Agent ID not found in {self.token_path}, deploying new Agent")
        else:
            print(f"agent_info.json not found at {self.token_path}, deploying new Agent")

        resp = self.signer.deploy_nft(
            nft_id=agent_id,
            nft_value=self.nft_cfg["value"] or 5,
            nft_data=json.dumps({"agent_name": self.alias}),
        )
        if resp.get("error"):
            raise RuntimeError(f"NFT deployment failed: {resp['error']}")
        nft_address = resp["nft_address"]
        if nft_address is None:
            raise RuntimeError("unexpected error during Agent deployment: unable to fetch Agent ID")

        agent_info.append({
            "agent_id":   nft_address,
            "agent_did":  self.signer.did,
            "agent_name": self.alias,
        })
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(agent_info, f, indent=2)
        print("Stored Agent info in:", self.token_path)
        return nft_address

    def _execute_nft(self, nft_address: str, payload: Any) -> Dict[str, Any]:
        nft_data = json.dumps(payload)
        print("NFT address:", nft_address)
        print("NFT data:", nft_data)
        try:
            response = self.signer.execute_nft(nft_address=nft_address, nft_data=nft_data)
        except Exception as e:
            raise RuntimeError(f"Rubix execute_nft call failed: {e}")
        if not response.get("status", False):
            raise RuntimeError(f"NFT Execution Failed: {response.get('message', '<no message>')}")
        return response

    def _build_nft_payload(self, remote_name: str) -> Dict[str, Any]:
        host_block = None
        user_block = None
        responses: List[Dict[str, Any]] = []

        for entry in self.last_parts:
            if not host_block and entry.get("host"):
                host_block = entry["host"]
                # Pull the embedded user_block (if any) out of the host envelope
                # so it sits at the top of the audit record next to host/responses.
                host_env = host_block.get("envelope") if isinstance(host_block, dict) else None
                if isinstance(host_env, dict) and isinstance(host_env.get("user_block"), dict):
                    user_block = host_env["user_block"]
            if entry.get("agent"):
                agent_entry = copy.deepcopy(entry["agent"])
                env = agent_entry.get("envelope", {}) or {}
                env["host_trust_issues"] = self.last_trust_issues
                agent_entry["agent_did"] = agent_entry.get("agent")
                agent_entry["agent"] = remote_name
                agent_entry["envelope"] = env
                responses.append(agent_entry)

        # If a user_block was carried, the writing DNA is treated as the user
        # who owns the audit-log NFT. Otherwise this falls back to the legacy
        # "host_agent" framing for back-compat.
        executor = "user" if user_block is not None else "host_agent"

        payload: Dict[str, Any] = {
            "comment":  f"Agent communication initiation to {remote_name}",
            "executor": executor,
            "did":      self.did,
            "verification": {
                "status":       self.last_verification_status,
                "trust_issues": self.last_trust_issues,
            },
        }
        if user_block is not None:
            payload["user"] = user_block
        payload["host"] = host_block
        payload["responses"] = responses
        return payload

    # ─── Helpers for verify_reply() ──────────────────────────────────────────

    @staticmethod
    def _unwrap_signed_text(raw) -> Optional[str]:
        """
        Pull the signed combined_json out of whatever transport handed us.
        Accepts:
          - plain combined_json string (returned as-is)
          - JSON-stringified ``{"combined_json": "..."}`` (one level of unwrap)
          - dict already parsed (same unwrap)
          - list of A2A-style parts (``[{"text": "..."}, ...]`` or list of
            strings): walks the list and returns the first part whose body
            parses to a signed envelope (a dict with a top-level ``"agent"``
            block).
        """
        if raw is None:
            return None

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    text = item
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                else:
                    continue
                if AgentDNA._looks_signed(text):
                    return text
            return None

        if isinstance(raw, dict):
            inner = raw.get("combined_json")
            return inner if isinstance(inner, str) else json.dumps(raw)

        text = (raw or "").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("combined_json"), str):
                return obj["combined_json"]
        except Exception:
            pass
        return text

    @staticmethod
    def _looks_signed(text: str) -> bool:
        """True iff ``text`` parses to a dict carrying a signed agent block."""
        if not isinstance(text, str):
            return False
        stripped = text.lstrip()
        if not stripped.startswith("{"):
            return False
        try:
            obj = json.loads(text)
        except Exception:
            return False
        if not isinstance(obj, dict):
            return False
        agent = obj.get("agent")
        return isinstance(agent, dict) and "envelope" in agent

    @staticmethod
    def _infer_remote_name(signed_text: str) -> Optional[str]:
        """
        Best-effort remote_name extraction from the signed reply: prefer an
        explicit ``agent_name`` / ``remote_name`` field inside the envelope,
        else fall back to the agent's DID tail (last 16 chars).
        """
        try:
            obj = json.loads(signed_text)
        except Exception:
            return None
        agent_block = obj.get("agent") if isinstance(obj, dict) else None
        if not isinstance(agent_block, dict):
            return None
        env = agent_block.get("envelope") or {}
        for key in ("agent_name", "remote_name"):
            if isinstance(env.get(key), str) and env[key]:
                return env[key]
        did = agent_block.get("agent")
        if isinstance(did, str) and did:
            return did.split(":")[-1][:16]
        return None
