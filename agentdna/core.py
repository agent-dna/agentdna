from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union, cast

from multiformats_cid.cid import CIDv0

from .trust import RubixTrustService

EPOCH_THRESHOLD_SECONDS = 3600
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

    cfg: Dict[str, Any] = {}
    try:
        with Path(config_path).open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    cfg_nft = cfg.get("nft") or {}

    return {
        "value":       float(os.getenv("NFT_VALUE", cfg_nft.get("value", 0.001))),
        "data":        os.getenv("NFT_INIT_DATA", cfg_nft.get("data", "init data")),
        "password":    cfg_nft.get("password"),
        "timeout":     float(cfg_nft.get("timeout", 100.0)),
        "quorum_type": int(cfg_nft.get("quorum_type", 2)),
        "chain_url":   os.getenv("CHAIN_URL") or cfg.get("chain_url"),
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

    VALID_KINDS = ("user", "agent")

    def __init__(
        self,
        alias: str,
        api_key: str,
        chain_url: Optional[str] = None,
        token_filename: str = "agent_info.json",
        enable_nft: bool = True,
        cbac: bool = False,
        card_nft: Optional[str] = None,
        kind: str = "agent",
        policy_file: Optional[Union[str, Path]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        deployer_did: Optional[str] = None,
        **_legacy,
    ) -> None:
        # Back-compat: silently drop role= from older callers
        _legacy.pop("role", None)
        # Back-compat: older callers used skill_md=; accept it as an alias.
        if "skill_md" in _legacy and policy_file is None:
            policy_file = _legacy.pop("skill_md")
        _legacy.pop("skill_md", None)

        if kind not in self.VALID_KINDS:
            raise ValueError(
                f"kind={kind!r} not in {self.VALID_KINDS} — "
                "human principals use 'user', software agents use 'agent'"
            )
        # Users sign intents at the top of the chain; they don't hold policy
        # files. Agents act on behalf of users; they don't carry user metadata.
        # Reject the wrong-side combinations up front.
        if kind == "user" and (card_nft or policy_file):
            raise ValueError(
                "kind='user' cannot carry card_nft= or policy_file= "
                "(policies govern agent actions, not user intents)"
            )

        if card_nft and policy_file:
            raise ValueError("pass either card_nft= or policy_file=, not both")

        # Trust layer — sign/verify primitives via rubix-py.
        # Resolution order: explicit arg → CHAIN_URL env var → config.json chain_url.
        if chain_url is None:
            nft_cfg_early = _load_nft_config()
            chain_url = nft_cfg_early.get("chain_url")
        self.trust = RubixTrustService(alias=alias, api_key=api_key, chain_url=chain_url)
        self.did = self.trust.did
        self.signer = self.trust.signer

        self.alias = alias
        self.kind = kind
        self.token_filename = token_filename

        # CBAC opt-in. When `cbac=True`, handle() runs the policy chain check
        # alongside CoCA verification. `card_nft` is this agent's own policy
        # card NFT hash — automatically attached to every envelope build()
        # produces, so downstream verifiers can fetch and enforce it.
        self.cbac_enabled = bool(cbac)
        self.card_nft = card_nft
        self._cbac_engine = None        # lazily created when CBAC is invoked

        # Kind-specific identity-NFT payload inputs:
        #   user:  metadata        (free-form profile dict, defaults to {})
        #   agent: policy  (base64-encoded contents of the file at
        #                      policy_file, "" if none provided). The
        #                      file is treated as opaque bytes — projects
        #                      choose their own format (markdown, JSON,
        #                      plain text, custom DSL).
        self.metadata: Dict[str, Any] = dict(metadata) if metadata is not None else {}
        self.deployer_did: Optional[str] = deployer_did

        # if kind == "agent":
        #     if policy_file is not None:
        #         self.policy = base64.b64encode(
        #             Path(policy_file).read_bytes()
        #         ).decode("ascii")
        #     else:
        #         raise ValueError("kind='agent' requires a policy_file to be provided")
        
        # NFT config + per-call audit state
        self.enable_nft = enable_nft
        self.nft_cfg = _load_nft_config()
        self.last_parts: List[Dict[str, Any]] = []
        self.last_trust_issues: List[str] = []
        self.last_verification_status: str = "unknown"  # "ok" | "failed" | "unknown"
        self.last_cbac_summary: Optional[Dict[str, Any]] = None  # from resource's reply

        self.token_path: Optional[Path] = None
        self.nft_token: Optional[str] = None

        # Eagerly publish the kind-specific identity NFT so the on-chain
        # record exists from construction. Branch is explicit so a reader of
        # __init__ sees exactly which deploy runs for which kind. Idempotent
        # across re-runs via agent_info.json. Pass enable_nft=False for pure
        # signers (hosts/remotes that don't own an identity record).
        if self.enable_nft:
            if self.kind == "user":
                self.deploy_user_nft()

        # Intent NFT Ops
        self.current_nft_intent_id = ""
        self.current_nft_intent_epoch = 0
        self._last_chain_depth: int = 0

    def update_agent_policy(self, agent_id, policy_content):
        """
        For kind='agent', update the on-chain identity NFT with the current
        policy file contents. Does nothing for kind='user' (users don't have
        policies).

        Raises RuntimeError if this AgentDNA is kind='user' or if the NFT
        hasn't been deployed yet.
        """

        # Query agent policy history from provenance layer
        agent_history = self.history_agent(agent_id, latest=True)
        if not agent_history:
            raise RuntimeError(f"No history found for agent NFT {agent_id}; cannot update policy")

         if len(agent_history) == 0:
            raise RuntimeError(
                f"Agent NFT {agent_id} has no history; cannot update policy"
            )

        agent_info = agent_history[-1]["data"]

        if "policy" not in agent_info:
            raise RuntimeError(f"Agent NFT {agent_id} history missing 'policy' field; cannot update policy, actual agent_info: {agent_info}")

        # encode the policy to base64 and store it
        agent_info["policy"] = base64.b64encode(policy_content.encode("utf-8")).decode("ascii")

        try: 
            self._execute_nft(nft_address=agent_id, payload=agent_info)
            print("Policy updated for agent: ", agent_id)
        except Exception as e:
            raise RuntimeError(f"Failed to update policy on-chain: {e}")

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

    # ─── Identity NFT deployment (kind-specific helpers live in agent/user) ─

    def deploy_agent_card(self) -> str:
        """Publish the agent's identity NFT. See ``agent.deploy_card``."""
        from .agent import deploy_card
        return deploy_card(self)

    def deploy_user_nft(self) -> str:
        """Publish the user's identity NFT. See ``user.deploy_user_nft``."""
        from .user import deploy_user_nft
        return deploy_user_nft(self)

    def deploy_agent_nft(
        self,
        agent: "AgentDNA",
        metadata: Dict[str, Any] = {},
        *,
        policy_content: str,
    ) -> str:
        """
        Deploy an agent's identity NFT using THIS principal's wallet (user or admin).

        The caller (self) pays for and signs the Rubix transaction; the agent's DID
        is embedded as ``agent_did`` and ``self.did`` is recorded as ``deployer``.
        The resulting NFT address is stored in agent_info.json and set on ``agent.nft_token``.

        The agent must have been created with ``policy_file=`` (sets ``agent.policy``).
        Pass ``policy_content=`` to override with a rendered version of the template
        (substituted DIDs, dates etc.) before storing on chain — this is the common case
        when skill.md files contain ``${DID}`` / ``${DATE}`` placeholders.
        """
        if self.kind != "user":
            raise RuntimeError(
                f"deploy_agent_nft() must be called on a user principal; this is kind={self.kind!r}"
            )

        if policy_content == "":
            raise ValueError("policy_content cannot be empty, pass the file contents as a string")

        policy = base64.b64encode(policy_content.encode("utf-8")).decode("ascii")
        
        agent.deployer_did = self.did

        # Ensure token_path is set on the agent
        if agent.token_path is None:
            token_dir = Path.home() / ".agentdna"
            token_dir.mkdir(parents=True, exist_ok=True)
            agent.token_path = token_dir / agent.token_filename

        # Build the agent NFT payload
        from .agent import identity_payload
        nft_payload = identity_payload(self.did, metadata, policy)

        # Derive deterministic NFT id from agent's did.alias
        digest = hashlib.sha256(f"{self.did}.{self.alias}".encode()).digest()
        multihash_bytes = bytes([0x12, len(digest)]) + digest
        agent_id = CIDv0(multihash_bytes).encode().decode("utf-8")

        # Check cache first
        agent_info: List[Dict[str, Any]] = []
        if agent.token_path.exists():
            try:
                with open(agent.token_path, "r", encoding="utf-8") as f:
                    agent_info = json.load(f)
            except Exception:
                agent_info = []
            for entry in agent_info:
                if entry.get("agent_id") == agent_id:
                    print(f"Using existing agent NFT for {agent.alias}: {agent_id}")
                    agent.nft_token = agent_id
                    return agent_id

        # Deploy using THIS principal's trust layer (self.trust = deployer's signer)
        resp = self.trust.deploy_nft(
            nft_id=agent_id,
            nft_value=self.nft_cfg.get("value") or 0.001,
            nft_data=json.dumps(nft_payload),
        )
        if resp.get("error"):
            raise RuntimeError(f"Agent NFT deployment failed for {self.alias}: {resp['error']}")
        nft_address = resp.get("nft_address")
        if nft_address is None:
            raise RuntimeError(f"Unexpected error deploying NFT for {self.alias}: no address returned")

        agent_info.append({
            "agent_id":   nft_address,
            "agent_did":  self.signer.did,
            "agent_name": self.alias,
        })
        with open(agent.token_path, "w", encoding="utf-8") as f:
            json.dump(agent_info, f, indent=2)

        agent.nft_token = nft_address
        print(f"✅ Deployed agent NFT for {agent.alias} (deployer: {self.alias}): {nft_address}")
        return nft_address

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
        # New format: payload.response.  Legacy fallback: flat response field.
        env_payload = env.get("payload") or {}
        body = env_payload.get("response") or env.get("response")
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

            # New format: payload.message.  Legacy fallback: original_message flat.
            raw_intent = (
                (user_env.get("payload") or {}).get("message")
                or user_env.get("original_message")
            )
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

        # rubix-py may not find original_message when the new envelope format is
        # used (payload.message instead of flat original_message).  Fall back to
        # extracting it from the host block's typed payload.
        orig_msg = info.get("original_message") or ""
        if not orig_msg and isinstance(host_block, dict):
            orig_msg = (
                (host_block.get("envelope") or {})
                .get("payload", {})
                .get("message", "")
            )

        ctx = RequestContext(
            original_message=orig_msg,
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

    # ─── One-shot helpers ───────────────────────────────────────────────────

    async def initialise_intent(
        self,
        payload: Union[str, dict, list],
        *,
        state: Optional[Dict[str, Any]] = None,
    ) -> Tuple["SignedEnvelope", "RequestContext"]:
        """
        Sign an intent and self-verify in one call.

        Folds the usual two-step::

            env = dna.build(payload)
            ctx = await dna.handle(env)

        into a single ``await``. Returns ``(envelope, ctx)``:

          - ``envelope`` — the wire-ready ``SignedEnvelope`` to forward
            downstream.
          - ``ctx`` — the ``RequestContext`` from running ``handle()``
            against what we just signed, so the caller can log / inspect
            / display what's been committed before sending.

        Typical use is at the top of a chain — a ``kind='user'`` principal
        initialising a new intent — but nothing restricts it to users;
        any AgentDNA that originates an intent can use it.
        """

        if self.kind != "user":
            raise RuntimeError("initialise_intent is only for kind='user' principals")
        
        if self.nft_token is None:
            raise RuntimeError(
                "NFT not deployed yet — initialise_intent requires the user's identity NFT to exist so it can write the first audit record."
            )

        if self.current_nft_intent_id == "" or int(time.time()) - self.current_nft_intent_epoch > EPOCH_THRESHOLD_SECONDS:
            child_nft_details = self.trust.deploy_child_nft(self.nft_token, "{}")
            self.current_nft_intent_id = child_nft_details["childNFTId"]
            self.current_nft_intent_epoch = int(time.time())

        # build(positional payload, no ctx) always returns SignedEnvelope;
        # handle(positional envelope, no original=) always returns
        # RequestContext. The dispatching unions on those signatures hide
        # that from the type checker, so narrow explicitly.
        envelope = cast(SignedEnvelope, self.build(payload, state=state))
        ctx = cast(RequestContext, await self.handle(envelope))
        return envelope, ctx

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

    def history_agent(self, agent_id: str, latest: bool = False) -> List[Dict[str, Any]]:
        """Decoded NFT chain log for the given NFT ID.

        Returns ``[]`` if the NFT hasn't been deployed yet. JSON-string fields
        (``NFTData``, ``data``, nested ``original_message``, …) are parsed
        recursively so renderers like ``st.json()`` show a foldable tree
        rather than one escaped blob.
        """
        if not agent_id:
            return []

        # Lazy import — keeps the chain-query dep optional at import time.
        from rubix.client import RubixClient
        from rubix.querier import Querier

        client = RubixClient(node_url=self.trust.base_url, timeout=300)
        history = Querier(client).get_nft_states(nft_address=agent_id, only_latest_state=latest)

        return [_deep_json_decode(s) for s in history]

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
        
        child_nfts = Querier(client).get_child_nfts(self.nft_token)

        states = []

        for child in child_nfts:
            state = Querier(client).get_nft_states(
                nft_address=child["nft_id"],
                only_latest_state=latest,
            )

            for s in state:
                states.append(s)

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
        downstream: Optional[Union["VerifyResult", "SignedEnvelope", Dict[str, Any]]] = None,
        state: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        recipient_name: Optional[str] = None,
        block_type: Optional[str] = None,
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
                recipient_name=recipient_name,
                block_type=block_type,
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
            # For middle agents: extract the downstream response block to use
            # as parent_block, so the return chain nests correctly per spec.
            parent_block_override: Optional[Dict[str, Any]] = None
            if downstream is not None:
                if isinstance(downstream, VerifyResult):
                    parent_block_override = downstream.agent_block
                elif isinstance(downstream, SignedEnvelope):
                    parent_block_override = downstream.host_block
                elif isinstance(downstream, dict) and all(
                    k in downstream for k in ("agent", "envelope", "signature")
                ):
                    parent_block_override = downstream
            built = self._build_agent_response(
                original_message=ctx.original_message or response_str,
                response=response_str,
                host_block=ctx.host_block,
                parent_block=parent_block_override,
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
            recipient_name=recipient_name,
            block_type=block_type,
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
        recipient_name: Optional[str] = None,
        block_type: Optional[str] = None,
        **_extra,
    ) -> Dict[str, Any]:
        state = state or {}
        task_id = state.get("task_id") or str(uuid.uuid4())
        context_id = state.get("context_id") or str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        ts = datetime.utcnow().isoformat() + "Z"

        # Determine block type from kind + parent presence when not explicit.
        if block_type is None:
            if self.kind == "user":
                block_type = "intent"
            elif parent_block is not None:
                block_type = "delegate"
            else:
                block_type = "intent"

        # Auto-derive received_from from the parent block's signer name.
        received_from = ""
        if parent_block is not None and isinstance(parent_block, dict):
            received_from = parent_block.get("name") or ""

        # Build the spec-compliant typed payload.
        spec_payload: Dict[str, Any] = {
            "message":          original_message,
            "ts":               ts,
            # Internal echo used by the response-side verification check.
            # Not shown in the NFT chain but covered by this block's signature.
            "original_message": original_message,
        }
        if block_type == "intent":
            if recipient_name:
                spec_payload["delegate_to"] = recipient_name
        elif block_type in ("delegate", "execute"):
            spec_payload["decision"] = block_type
            if received_from:
                spec_payload["received_from"] = received_from
            if recipient_name:
                spec_payload["delegate_to"] = recipient_name

        host_envelope: Dict[str, Any] = {
            "payload":     spec_payload,
            "_task_id":    task_id,
            "_context_id": context_id,
            "_message_id": message_id,
        }
        if self.card_nft is not None:
            host_envelope["agent_card_nft"] = self.card_nft
        if parent_block is not None:
            host_envelope["parent_block"] = parent_block

        host_block = self.trust.sign_envelope(host_envelope)
        # Block-level metadata — not covered by the signature per spec.
        host_block["name"]         = self.alias
        host_block["direction"]    = "outbound"
        host_block["type"]         = block_type
        host_block["verification"] = {"signature_valid": True, "trust_issues": []}

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
        parent_block: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
        **_extra,
    ) -> Dict[str, Any]:
        # Determine the actual block that becomes parent_block in the envelope.
        # When a middle agent wraps a downstream response, caller passes
        # parent_block explicitly; otherwise fall back to host_block (simple case).
        actual_parent = parent_block if parent_block is not None else host_block

        # Derive verified_upstream — name of the agent whose signature this
        # block is vouching for.
        #   - If parent is our own outbound block (execute), the agent we
        #     verified is the one who delegated TO us (host_block).
        #   - Otherwise the agent we verified is the one whose block we're
        #     wrapping (actual_parent).
        verified_upstream = ""
        if actual_parent is not None and isinstance(actual_parent, dict):
            parent_signer = actual_parent.get("agent", "")
            if parent_signer == self.did:
                # Parent is our own execute block — verified the upstream caller.
                if isinstance(host_block, dict):
                    verified_upstream = host_block.get("name") or host_block.get("agent", "")
            else:
                verified_upstream = actual_parent.get("name") or parent_signer
        elif isinstance(host_block, dict):
            verified_upstream = host_block.get("name") or host_block.get("agent", "")

        spec_payload: Dict[str, Any] = {
            "response":          response,
            "verified_upstream": verified_upstream,
            # Internal echo used by the caller's verification check.
            # Covered by this block's signature; omitted from the NFT chain.
            "original_message":  original_message,
        }

        # Lift CBAC attestation into the spec payload; leave other extra fields
        # (e.g. host_trust_issues) in the envelope but outside payload.
        extra = extra or {}
        if "cbac" in extra:
            spec_payload["cbac"] = extra.pop("cbac")

        envelope: Dict[str, Any] = {"payload": spec_payload}
        if extra:
            envelope.update(extra)
        if actual_parent is not None:
            envelope["parent_block"] = actual_parent

        agent_block = self.trust.sign_envelope(envelope)
        agent_block["name"]         = self.alias
        agent_block["direction"]    = "inbound"
        agent_block["type"]         = "response"
        agent_block["verification"] = {"signature_valid": True, "trust_issues": []}

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

            # Support both new format (payload.original_message) and legacy flat field.
            echoed = (
                (env_verified.get("payload") or {}).get("original_message")
                or env_verified.get("original_message")
            )
            if echoed and echoed != original_task:
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
            # New format: payload.cbac.  Legacy fallback: flat cbac field.
            cbac_attest = (env.get("payload") or {}).get("cbac") or env.get("cbac")
            if isinstance(cbac_attest, dict):
                self.last_cbac_summary = cbac_attest
                break

        # Write the audit record to chain
        if self.enable_nft and execute_nft and verified:
            token = self.current_nft_intent_id
            if token != "":
                try:
                    nft_payload = self._build_nft_payload(remote_name)
                    nft_payload["type"] = "intent_nft"
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

    def _publish_identity_nft(self) -> str:
        """
        Shared internal used by ``deploy_card`` / ``deploy_user_nft``.

        Resolves ``self.token_path`` (cache file on disk), then delegates to
        ``_load_or_deploy_nft`` which builds the kind-specific payload via
        ``_identity_nft_payload``. Idempotent: returns the cached NFT
        address if this identity has already been published in-process or
        recorded in ``agent_info.json`` from a previous run.
        """
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
        print(f"✅ Rubix identity NFT for {self.kind} '{self.alias}':", self.nft_token)
        return self.nft_token

    def _ensure_nft_token(self) -> Optional[str]:
        """
        Gated dispatcher for the lazy handle()-driven deploy path. Picks the
        right kind-specific deploy so the audit-log write can lazily mint an
        identity NFT if one hasn't been published yet.
        """
        if not self.enable_nft:
            return None
        if self.kind == "user":
            return self.deploy_user_nft()
        return self.deploy_agent_card()

    def _load_or_deploy_nft(self) -> str:
        # Identity NFT id = CIDv0(sha256(did.alias))
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
                    print("Using existing Identity NFT from:", self.token_path)
                    return agent_id
            print(f"Identity NFT not found in {self.token_path}, deploying new {self.kind}")
        else:
            print(f"agent_info.json not found at {self.token_path}, deploying new {self.kind}")

        # Kind-specific identity payload — what gets stamped on chain.
        nft_payload = self._identity_nft_payload()

        resp = self.trust.deploy_nft(
            nft_id=agent_id,
            nft_value=self.nft_cfg["value"] or 0.001,
            nft_data=json.dumps(nft_payload),
        )
        if resp.get("error"):
            raise RuntimeError(f"NFT deployment failed: {resp['error']}")
        nft_address = resp["nft_address"]
        if nft_address is None:
            raise RuntimeError("unexpected error during identity NFT deployment: unable to fetch id")

        agent_info.append({
            "agent_id":   nft_address,
            "agent_did":  self.signer.did,
            "agent_name": self.alias
        })
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(agent_info, f, indent=2)
        print("Stored identity info in:", self.token_path)
        return nft_address

    def _identity_nft_payload(self, policy: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatch to the kind-specific payload builder in agent.py/user.py."""
        if self.kind == "user":
            from .user import identity_payload
            return identity_payload(self)
        else:
            from .agent import identity_payload
            agent_did = self.did
            if metadata is None:
                metadata = {}

            if policy is None:
                policy = ""

            return identity_payload(agent_did=agent_did, policy_content=policy, agent_metadata=metadata)

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
        trust_issues = list(self.last_trust_issues or [])

        # Collect the first valid outbound (host) and inbound (agent) blocks.
        host_block: Optional[Dict[str, Any]] = None
        response_block: Optional[Dict[str, Any]] = None
        response_sig_valid = False

        for entry in self.last_parts:
            if not host_block and entry.get("host"):
                host_block = entry["host"]
            if not response_block and entry.get("agent"):
                response_block = copy.deepcopy(entry["agent"])
                response_sig_valid = bool(entry.get("agent_sig_valid"))

        # Annotate the response block with spec block-level fields when they
        # weren't set by an older build() call (e.g. remote agent on old SDK).
        if response_block is not None:
            if not response_block.get("name"):
                response_block["name"] = remote_name
            if not response_block.get("direction"):
                response_block["direction"] = "inbound"
            if not response_block.get("type"):
                response_block["type"] = "response"
            if not response_block.get("verification"):
                response_block["verification"] = {
                    "signature_valid": response_sig_valid,
                    "trust_issues":    [],
                }
            # Ensure the response envelope's parent_block points to the
            # outbound chain when the remote agent didn't include it (old SDK).
            if host_block is not None:
                resp_env = response_block.setdefault("envelope", {})
                if "parent_block" not in resp_env:
                    resp_env["parent_block"] = host_block

        # Walk the outbound chain to find the root signer and total depth.
        outbound_chain = self._walk_chain(host_block) if host_block else []
        executor = "user" if len(outbound_chain) > 1 else "system"

        # Extract the response value to surface in the verify block.
        final_response = ""
        if response_block is not None:
            resp_env = response_block.get("envelope") or {}
            resp_payload = resp_env.get("payload") or {}
            final_response = resp_payload.get("response", "")

        # Build the outermost verify block: this agent/user signs the audit.
        verify_spec_payload: Dict[str, Any] = {
            "response":          final_response,
            "verified_upstream": remote_name,
            "nft_executed":      True,
        }
        verify_envelope: Dict[str, Any] = {"payload": verify_spec_payload}
        if response_block is not None:
            verify_envelope["parent_block"] = response_block

        verify_block = self.trust.sign_envelope(verify_envelope)
        verify_block["name"]         = self.alias
        verify_block["direction"]    = "inbound"
        verify_block["type"]         = "verify"
        verify_block["verification"] = {
            "signature_valid": True,
            "trust_issues":    trust_issues,
        }

        # Total chain depth = all blocks reachable from verify outward to root.
        chain_depth = len(self._walk_chain(verify_block))

        # Build a readable comment from the agent names in the chain.
        chain_names: List[str] = []
        for blk in reversed(outbound_chain):
            n = blk.get("name", "")
            if n and n not in chain_names:
                chain_names.append(n)
        if remote_name not in chain_names:
            chain_names.append(remote_name)
        comment = "Agent communication — " + " → ".join(chain_names)

        self._last_chain_depth = chain_depth

        return {
            "comment":  comment,
            "executor": executor,
            "did":      self.did,
            "verification": {
                "status":       self.last_verification_status,
                "chain_depth":  chain_depth,
                "trust_issues": trust_issues,
            },
            "chain": verify_block,
        }

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
