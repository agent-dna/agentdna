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
    Recursively parse JSON-string values into their objects.

    Used by ``AgentDNA.history()`` so chain records render as a fully
    foldable tree. Strings that aren't valid JSON are left untouched.
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
# Public types returned by build() and handle().
# ──────────────────────────────────────────────────────────────────────────────

class SignedEnvelope(str):
    """
    Wire-ready signed envelope returned by ``dna.build(payload)``.

    It behaves as the wire string itself; the signed block and ids hang off
    as attributes::

        env = dna.build({"tool": "append_task", "args": {...}})
        transport.send(env)              # env IS the wire string
        env.host_block                   # signed dict
        env.message_id, env.context_id   # ids
        env.original_message             # what we signed
        env.parent_block                 # upstream signer's signed block (delegation chain), if any
    """

    _ATTRS = ("host_block", "message_id", "context_id", "original_message", "parent_block")

    def __new__(cls, wire: str, **attrs):
        obj = super().__new__(cls, wire or "")
        for name in cls._ATTRS:
            object.__setattr__(obj, name, attrs.get(name))
        return obj


@dataclass
class VerifyResult:
    """Typed outcome of ``await dna.handle(reply, original=env)``."""

    payload: Optional[Any] = None                   # parsed reply body
    verified: bool = False                          # all signatures passed
    trust_issues: List[str] = field(default_factory=list)
    signed_text: Optional[str] = None               # the inner combined_json we verified
    host_block: Optional[Dict[str, Any]] = None
    agent_block: Optional[Dict[str, Any]] = None
    nft_result: Optional[Dict[str, Any]] = None
    verification_status: str = "unknown"            # "ok" | "failed" | "unknown"
    user_block: Optional[Dict[str, Any]] = None     # signed user envelope, if any
    user_verified: bool = False                     # user signature valid?


@dataclass
class RequestContext:
    """
    Typed outcome of ``await dna.handle(envelope)`` on the remote side.

    Pass it back into ``dna.build(payload, ctx=ctx)`` to sign a reply that's
    cryptographically bound to the exact request it answers.
    """

    original_message: str = ""                      # the JSON the host signed
    host_block: Optional[Dict[str, Any]] = None     # signed host block (echo back when replying)
    trust_issues: List[str] = field(default_factory=list)
    verified: bool = False
    user_block: Optional[Dict[str, Any]] = None     # signed user envelope, if any
    user_id: Optional[str] = None                   # user DID
    user_intent: Optional[str] = None               # what the user signed
    user_verified: bool = False                     # user signature valid?
    cbac_result: Optional[Any] = None               # CBACResult when handle(cbac=True)


# ──────────────────────────────────────────────────────────────────────────────
# AgentDNA — the single entry point. build() signs, handle() verifies.
# Sign/verify primitives live in RubixTrustService (rubix-py adapter).
# ──────────────────────────────────────────────────────────────────────────────

class AgentDNA:
    """
    Single entry point for adopters. Two methods do everything:

        env    = dna.build(payload)                       # sign outbound
        result = await dna.handle(reply, original=env)    # verify + write NFT

    Plus shortcuts:

        AgentDNA.from_env(alias="...")                    # construct from env vars
        dna.history()                                     # decoded chain log
    """

    # ─── Construction ────────────────────────────────────────────────────────

    def __init__(
        self,
        alias: str,
        api_key: str,
        chain_url: Optional[str] = None,
        token_filename: str = "agent_info.json",
        enable_nft: bool = True,
        cbac: bool = False,
        card_nft: Optional[str] = None,
        **_legacy,
    ) -> None:
        # Back-compat: silently drop role= from older callers
        _legacy.pop("role", None)

        # Trust layer — sign/verify primitives via rubix-py
        self.trust = RubixTrustService(alias=alias, api_key=api_key, chain_url=chain_url)
        self.did = self.trust.did
        self.signer = self.trust.signer

        self.alias = alias
        self.token_filename = token_filename

        # CBAC opt-in. When `cbac=True`, handle() runs the policy chain check
        # alongside CoCA verification. `card_nft` is this agent's own policy
        # card NFT hash — automatically attached to every envelope build()
        # produces, so downstream verifiers can fetch and enforce it.
        self.cbac_enabled = bool(cbac)
        self.card_nft = card_nft
        self._cbac_engine = None        # lazily created when CBAC is invoked

        # NFT config + per-call audit state
        self.enable_nft = enable_nft
        self.nft_cfg = _load_nft_config()
        self.last_parts: List[Dict[str, Any]] = []
        self.last_trust_issues: List[str] = []
        self.last_verification_status: str = "unknown"  # "ok" | "failed" | "unknown"
        self.last_cbac_summary: Optional[Dict[str, Any]] = None  # from resource's reply

        self.token_path: Optional[Path] = None
        self.nft_token: Optional[str] = None

        # Deploy the audit-log NFT eagerly so the chain identity exists from
        # construction. Cached in agent_info.json — re-runs reuse it.
        # Pass enable_nft=False for pure signers (hosts/remotes that don't
        # own the audit log).
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

    # ─── Internal verify helpers (used by handle() and the aliases) ──────────

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

        # Pull the reply body out of the signed envelope (parse JSON if we can).
        body = env.get("response")
        parsed_payload: Any = body
        if isinstance(body, str):
            try:
                parsed_payload = json.loads(body)
            except (TypeError, json.JSONDecodeError):
                parsed_payload = body  # not JSON — keep as plain string

        # If a delegation chain rode along, walk down to the root (the user)
        # and verify their signature. Intermediate signers are trusted via
        # the outer signature already verified by _handle_host_response.
        user_block = None
        user_verified = False
        if isinstance(host_block, dict):
            chain = self._walk_chain(host_block)
            if len(chain) > 1:
                user_block = chain[-1]
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

        # verify_message_payload always checks the host (outermost) signature
        # and, in heavy mode, also verifies the agent + responses blocks. It
        # does NOT walk the nested delegation chain — that's done below.
        info = self.trust.verify_message_payload(raw_text=raw_text, mode=verify_mode)

        host_block = info.get("host_block")
        trust_issues = list(info.get("trust_issues") or [])
        mode = (verify_mode or "light").lower()

        # Walk host → ... → user. chain[0] is host (already verified above),
        # chain[-1] is the root user (or the host itself for un-delegated calls).
        chain = self._walk_chain(host_block) if isinstance(host_block, dict) else []
        user_block: Optional[Dict[str, Any]] = chain[-1] if len(chain) > 1 else None

        user_id: Optional[str] = None
        user_intent: Optional[str] = None
        user_verified = False
        chain_ok = True

        if user_block is not None:
            # Heavy: verify every layer below host. Light: verify only the
            # root (user) — intermediate signers are trusted transitively
            # because each outer signature commits to the next inner block.
            levels_to_verify = chain[1:] if mode == "heavy" else [chain[-1]]

            for level in levels_to_verify:
                did = level.get("agent") if isinstance(level, dict) else None
                env = level.get("envelope") if isinstance(level, dict) else None
                sig = level.get("signature") if isinstance(level, dict) else None
                if not (did and sig and isinstance(env, dict)):
                    trust_issues.append("Chain block missing agent/envelope/signature")
                    chain_ok = False
                    continue
                try:
                    ok = bool(self.trust.verify_envelope(did, env, sig))
                except Exception:
                    ok = False
                if not ok:
                    trust_issues.append(f"Invalid signature from {did}")
                    chain_ok = False

            user_id = user_block.get("agent")
            user_env = user_block.get("envelope") or {}
            user_verified = chain_ok

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

        ctx = RequestContext(
            original_message=info.get("original_message") or "",
            host_block=host_block,
            trust_issues=trust_issues,
            verified=bool(info.get("verified")) and chain_ok,
            user_block=user_block,
            user_id=user_id,
            user_intent=user_intent,
            user_verified=user_verified,
        )

        # CBAC — runs only when this agent was constructed with cbac=True.
        # CoCA must pass first; otherwise CBAC would be checking unverified
        # signers.
        if self.cbac_enabled and ctx.verified:
            ctx.cbac_result = await self._cbac_verify(ctx)
            if ctx.cbac_result is not None and ctx.cbac_result.decision == "deny":
                ctx.trust_issues.append(
                    f"CBAC denied: {ctx.cbac_result.reason}"
                )

        return ctx

    async def _cbac_verify(self, ctx: "RequestContext"):
        """Soft-imported CBAC engine, instantiated once per AgentDNA."""
        if self._cbac_engine is None:
            from .cbac import CBAC
            self._cbac_engine = CBAC(trust=self.trust)
        return await self._cbac_engine.verify(ctx)

    # ─── Explicit-name aliases — all delegate to build() / handle() ─────────

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
        Decoded NFT chain log for this agent.

        Returns ``[]`` if the NFT hasn't been deployed yet. JSON-string fields
        (``NFTData``, ``data``, nested ``original_message``, …) are parsed
        recursively so renderers like ``st.json()`` show a foldable tree
        rather than one escaped blob.
        """
        if not self.nft_token:
            return []

        # Lazy import — keeps the chain-query dep optional at import time.
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

    # ─── build() — sign outbound envelopes ──────────────────────────────────

    def build(
        self,
        payload: Optional[Union[str, dict, list]] = None,
        *,
        ctx: Optional[RequestContext] = None,
        parent: Optional[Union[SignedEnvelope, RequestContext, Dict[str, Any]]] = None,
        state: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        **legacy,
    ) -> Union[SignedEnvelope, str, Dict[str, Any]]:
        """
        Sign an outbound envelope.

        Sign a fresh request (host side) — positional payload::

            env = dna.build(host_msg)              # SignedEnvelope (str-like)
            transport.send(str(env))

        Sign a reply under a verified context (remote side) — pass ``ctx=``::

            wire = dna.build(payload, ctx=ctx)     # wire string

        Pass ``parent=`` to embed the upstream signer's signed block — this is
        how the delegation chain nests. The current agent's signature commits
        to the entire parent block, so any prior signer's contribution can't
        be tampered with without breaking verification at this layer::

            # Top of chain: the user signs their intent.
            user_signed = user_dna.build({"intent": prompt})

            # Next hop: host wraps the user's signed block + its own task.
            env = host_dna.build(host_msg, parent=user_signed)

            # Deeper hop: a sub-agent forwards by wrapping whatever it
            # received. Pass the RequestContext directly — its host_block
            # already carries the full upstream chain.
            ctx     = await agent1_dna.handle(envelope)
            sub_env = agent1_dna.build(sub_task, parent=ctx)

        Legacy kwarg form (returns a dict)::

            built = dna.build(original_message=task)
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
                parent_block=self._extract_parent_block(parent) if parent is not None else None,
            )

        if payload is None:
            raise ValueError(
                "build() needs a payload — pass it positionally or use "
                "the legacy original_message= kwarg."
            )

        # Sign a reply (remote side) — under a verified request context
        if ctx is not None:
            if isinstance(payload, str):
                response_str = payload
            else:
                response_str = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            merged_extra = {"host_trust_issues": ctx.trust_issues, **(extra or {})}
            # If CBAC ran inside handle(), attest the decision in the signed
            # reply so the upstream auditor can surface it without re-running.
            if getattr(ctx, "cbac_result", None) is not None and "cbac" not in merged_extra:
                cr = ctx.cbac_result
                merged_extra["cbac"] = {
                    "decision": cr.decision,
                    "n_denied": sum(1 for c in cr.trace if not c.passed),
                }
            built = self._build_agent_response(
                original_message=ctx.original_message or response_str,
                response=response_str,
                host_block=ctx.host_block,
                extra=merged_extra,
            )
            return built["combined_json"]

        # Sign a fresh request (top-of-chain or forwarding) — returns SignedEnvelope
        if isinstance(payload, str):
            original_message = payload
        else:
            original_message = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        parent_block = self._extract_parent_block(parent) if parent is not None else None
        built = self._build_host_request(
            original_message=original_message,
            state=state or {},
            parent_block=parent_block,
        )
        return SignedEnvelope(
            built["host_json"],
            host_block=built["host_block"],
            message_id=built["message_id"],
            context_id=built["context_id"],
            original_message=original_message,
            parent_block=parent_block,
        )

    @staticmethod
    def _extract_parent_block(
        parent: Union[SignedEnvelope, "RequestContext", Dict[str, Any], None],
    ) -> Optional[Dict[str, Any]]:
        """
        Normalize ``parent`` into a ``{agent, envelope, signature}`` dict.

        Accepts:
          - ``SignedEnvelope`` from an upstream ``build(...)`` — uses its
            ``host_block``.
          - ``RequestContext`` from a verified inbound ``handle(...)`` — uses
            ``ctx.host_block`` (the immediate sender's signed block, which
            itself carries the full upstream chain).
          - Raw signed-block dict ``{agent, envelope, signature}``.
          - ``None`` — top of chain.
        """
        if parent is None:
            return None
        if isinstance(parent, SignedEnvelope):
            return parent.host_block
        if isinstance(parent, RequestContext):
            return parent.host_block
        if isinstance(parent, dict):
            if all(k in parent for k in ("agent", "envelope", "signature")):
                return parent
            raise ValueError("parent dict must contain agent / envelope / signature keys")
        raise TypeError(
            f"parent must be a SignedEnvelope, RequestContext, or signed-block dict; "
            f"got {type(parent).__name__}"
        )

    def _build_host_request(
        self,
        *,
        original_message: str,
        state: Optional[Dict[str, Any]] = None,
        parent_block: Optional[Dict[str, Any]] = None,
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
        if self.card_nft is not None:
            # CBAC: this agent's policy card NFT hash. Downstream verifiers
            # fetch the card and enforce its allowed-actions/constraints/etc.
            # The signature commits to it, so it can't be swapped after the
            # fact.
            host_envelope["agent_card_nft"] = self.card_nft
        if parent_block is not None:
            # Embed the parent's signed block inside this envelope so our
            # signature commits to it — tampering with any upstream signer's
            # contribution breaks verification at this layer.
            host_envelope["parent_block"] = parent_block

        host_block = self.trust.sign_envelope(host_envelope)
        host_json = json.dumps({"host": host_block}, separators=(",", ":"), sort_keys=True)

        return {
            "kind":         "host_request",
            "host_block":   host_block,
            "host_json":    host_json,
            "task_id":      task_id,
            "context_id":   context_id,
            "message_id":   message_id,
            "parent_block": parent_block,
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

    # ─── handle() — verify inbound envelopes ────────────────────────────────

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
        Verify an inbound envelope.

        Verify a signed reply (user/host side) — pass ``original=``. Returns
        ``VerifyResult`` and writes the audit-log NFT (``execute_nft=False``
        to skip)::

            result = await dna.handle(reply_text, original=env)
            result.payload, result.verified, result.trust_issues

        Verify an inbound request (remote side) — returns ``RequestContext``::

            ctx = await dna.handle(dna_envelope)
            ctx.original_message, ctx.host_block, ctx.verified

        Legacy kwarg form (returns a dict)::

            info   = await dna.handle(raw_text=...)
            result = await dna.handle(resp_parts=..., original_task=..., remote_name=...)
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

        # Verify a signed reply — compare against what we sent (`original=`)
        if original is not None:
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

        # Verify an inbound request (remote side) → RequestContext
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

        # Snapshot for the NFT payload + status flag
        self.last_parts = verified
        self.last_trust_issues = trust_issues or []
        if not verified:
            self.last_verification_status = "failed"
        else:
            self.last_verification_status = (
                "failed" if self.last_trust_issues else "ok"
            )

        # Pull CBAC summary from the first reply that carries one (the resource
        # auto-attaches `cbac` in build(ctx=ctx) when its CBAC engine ran).
        self.last_cbac_summary = None
        for entry in verified:
            env = (entry.get("agent") or {}).get("envelope") or {}
            cbac_attest = env.get("cbac")
            if isinstance(cbac_attest, dict):
                self.last_cbac_summary = cbac_attest
                break

        # Write the audit record to chain
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

    # ─── NFT: deploy + execute ──────────────────────────────────────────────

    def _ensure_nft_token(self) -> Optional[str]:
        """Deploy (or load) the audit-log NFT. Cached after first call."""
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
        intermediates: List[Dict[str, Any]] = []
        chain_depth = 0
        responses: List[Dict[str, Any]] = []

        for entry in self.last_parts:
            if not host_block and entry.get("host"):
                host_block = entry["host"]
                # Walk the delegation chain inside host_block:
                #   chain[0]   = host (outermost)
                #   chain[-1]  = root user (when len > 1)
                #   chain[1:-1] = intermediate sub-agents (depth >= 3)
                chain = self._walk_chain(host_block)
                chain_depth = len(chain)
                if chain_depth > 1:
                    user_block = chain[-1]
                if chain_depth > 2:
                    intermediates = chain[1:-1]
            if entry.get("agent"):
                agent_entry = copy.deepcopy(entry["agent"])
                env = agent_entry.get("envelope", {}) or {}
                env["host_trust_issues"] = self.last_trust_issues
                agent_entry["agent_did"] = agent_entry.get("agent")
                agent_entry["agent"] = remote_name
                agent_entry["envelope"] = env
                responses.append(agent_entry)

        # With a user_block the writer is the user (top of chain). Without
        # one, fall back to "host_agent" for back-compat.
        executor = "user" if user_block is not None else "host_agent"

        payload: Dict[str, Any] = {
            "comment":  f"Agent communication initiation to {remote_name}",
            "executor": executor,
            "did":      self.did,
            "verification": {
                "status":       self.last_verification_status,
                "trust_issues": self.last_trust_issues,
                "chain_depth":  chain_depth,
            },
        }
        if self.last_cbac_summary is not None:
            payload["cbac"] = self.last_cbac_summary
        if user_block is not None:
            payload["user"] = user_block
        if intermediates:
            payload["intermediate_agents"] = intermediates
        payload["host"] = host_block
        payload["responses"] = responses
        return payload

    # ─── Helpers for handle() ────────────────────────────────────────────────

    @staticmethod
    def _walk_chain(signed_block: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Walk a nested delegation chain from outermost to root.

        Each layer is a signed block ``{agent, envelope, signature}`` whose
        envelope may contain a ``parent_block`` field carrying the previous
        signer's full signed block. The chain bottoms out when an envelope
        has no ``parent_block``.

        Returns ``[outermost, ..., root]``. The outermost is the immediate
        sender; the root is the original user. At depth-1 (no delegation)
        the list has a single entry.
        """
        chain: List[Dict[str, Any]] = []
        cur = signed_block
        # Hard cap to avoid pathological self-referential payloads.
        for _ in range(64):
            if not isinstance(cur, dict):
                break
            chain.append(cur)
            env = cur.get("envelope")
            cur = env.get("parent_block") if isinstance(env, dict) else None
        return chain

    @staticmethod
    def _unwrap_signed_text(raw) -> Optional[str]:
        """
        Pull the signed combined_json out of whatever the transport handed us:
          - plain combined_json string  → returned as-is
          - ``{"combined_json": "..."}`` dict or JSON string  → unwrapped
          - list of A2A-style parts (``[{"text": "..."}, ...]``)  → returns
            the first part whose body parses to a signed envelope (dict with
            a top-level ``"agent"`` block).
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
        """True if ``text`` parses to a dict with a signed agent block."""
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
        Best-effort remote name from the signed reply: prefer an explicit
        ``agent_name`` / ``remote_name`` field in the envelope, else fall
        back to the last 16 chars of the agent's DID.
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


# ──────────────────────────────────────────────────────────────────────────────
# Admin: deploy a CBAC card NFT (standalone tooling, mirrors user enrollment)
# ──────────────────────────────────────────────────────────────────────────────

def deploy_card(
    admin_dna: "AgentDNA",
    skill_md_path: Union[str, Path],
) -> str:
    """
    Admin signs and deploys a skill.md as a card NFT. Returns the
    NFT address. The card's ``issued-by`` field must match the
    admin's DID.

    Standalone admin tooling — runs out-of-band, same shape as user
    enrollment. Independent of the CBAC verify path.
    """
    from .cbac import parse_skill_md

    text = Path(skill_md_path).read_text(encoding="utf-8")
    card = parse_skill_md(text)

    if card.issued_by != admin_dna.did:
        raise ValueError(
            f"Card issued-by={card.issued_by!r} does not match admin DID "
            f"{admin_dna.did!r}"
        )

    # Deterministic NFT id from (admin DID, agent DID, content hash) so
    # re-running the same card on the same admin/agent is idempotent.
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    seed = f"card.{admin_dna.did}.{card.agent_did}.{content_hash[:16]}"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    multihash_bytes = bytes([0x12, len(digest)]) + digest
    nft_id = CIDv0(multihash_bytes).encode().decode("utf-8")

    # Admin signs the card content; signature ships inside the NFT so
    # offline verifiers can confirm the card came from this admin.
    admin_signature = admin_dna.trust.sign_envelope({"skill_md": text})

    nft_data = json.dumps({
        "skill_md":        text,
        "admin_signature": admin_signature,
    })

    resp = admin_dna.signer.deploy_nft(
        nft_id=nft_id,
        nft_value=0.001,
        nft_data=nft_data,
    )
    if resp.get("error"):
        raise RuntimeError(f"Card NFT deployment failed: {resp['error']}")
    nft_address = resp.get("nft_address")
    if not nft_address:
        raise RuntimeError("Card NFT deployment returned no nft_address")

    return nft_address
