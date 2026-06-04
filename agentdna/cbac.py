"""
CBAC — Context-Based Access Control for AgentDNA.

CBAC is the second pillar of the paper (§4.2). Where CoCA proves *who*
acted, CBAC proves *what they were allowed to do*.

Each agent carries a signed skill.md "card" — deployed as an NFT on
Rubix by the IT admin — and attaches the card's NFT hash to every
envelope it signs. When a request reaches a resource, the CBAC engine
walks the chain (CoCA), fetches each layer's card, and runs
deterministic policy checks:

  - is the action in `allowed-actions`?
  - is it in `forbidden-actions` (overrides allowed)?
  - do the args fit `constraints`?
  - for forwarding hops, is the next agent in `can-delegate-to`?
  - are the `requires` preconditions satisfied?

v1 is deterministic-only. V2: The gray-zone LLM check that reads the
markdown body

Usage
-----

    # Construction — set on the AgentDNA instance
    dna = AgentDNA(
        alias="FlightAgent",
        api_key=API_KEY,
        cbac=True,
        card_nft="Qm...flight...",
    )

    # Admin — deploy a card from a skill.md file (standalone helper,
    # outside the CBAC engine — same pattern as user enrollment).
    nft_hash = deploy_card(admin_dna, "skills/flight.md")

    # Inbound — CoCA + CBAC run inside handle() when cbac=True
    ctx = await dna.handle(envelope)
    ctx.cbac_result.decision    # "allow" | "deny" | "advise"
    ctx.cbac_result.trace       # per-layer card checks
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
import yaml

from .trust import RubixTrustService


# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses returned to adopters
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Card:
    """Parsed skill.md card. Frontmatter fields + the markdown body."""
    agent_did:         str
    agent_name:        str
    issued_by:         str
    issued_at:         datetime
    expires_at:        datetime
    allowed_actions:   List[str]
    forbidden_actions: List[str] = field(default_factory=list)
    constraints:       Dict[str, Any] = field(default_factory=dict)
    can_delegate_to:   List[str] = field(default_factory=list)
    requires:          Dict[str, Any] = field(default_factory=dict)
    body:              str = ""
    nft_address:       Optional[str] = None
    raw_frontmatter:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class CardCheck:
    """Result of CBAC check for one layer in the chain."""
    layer_did:    str
    card_nft:     Optional[str]
    card:         Optional[Card]
    action:       Optional[str]
    passed:       bool
    reasons:      List[str] = field(default_factory=list)


@dataclass
class CBACResult:
    """Overall CBAC decision after walking the full chain."""
    decision:    str                                # "allow" | "deny" | "advise"
    reason:      str = ""
    trace:       List[CardCheck] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# skill.md parsing
# ──────────────────────────────────────────────────────────────────────────────

REQUIRED_FRONTMATTER_KEYS = (
    "agent-did", "issued-by", "issued-at", "expires-at", "allowed-actions",
)


def parse_skill_md(text: str) -> Card:
    """
    Parse a skill.md (YAML frontmatter + markdown body) into a Card.

    Raises ValueError for any structural problem.
    """
    if not text.startswith("---"):
        raise ValueError("skill.md must start with YAML frontmatter delimited by ---")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("skill.md must have a closing --- line after frontmatter")
    frontmatter_text = parts[1]
    body = parts[2].lstrip("\n")

    fm = yaml.safe_load(frontmatter_text)
    if not isinstance(fm, dict):
        raise ValueError("skill.md frontmatter must be a YAML object")

    missing = [k for k in REQUIRED_FRONTMATTER_KEYS if k not in fm]
    if missing:
        raise ValueError(f"skill.md missing required field(s): {', '.join(missing)}")

    return Card(
        agent_did=fm["agent-did"],
        agent_name=fm.get("agent-name", ""),
        issued_by=fm["issued-by"],
        issued_at=_parse_dt(fm["issued-at"]),
        expires_at=_parse_dt(fm["expires-at"]),
        allowed_actions=list(fm.get("allowed-actions") or []),
        forbidden_actions=list(fm.get("forbidden-actions") or []),
        constraints=dict(fm.get("constraints") or {}),
        can_delegate_to=list(fm.get("can-delegate-to") or []),
        requires=dict(fm.get("requires") or {}),
        body=body,
        raw_frontmatter=fm,
    )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    raise ValueError(f"unparseable timestamp: {value!r}")


# ──────────────────────────────────────────────────────────────────────────────
# CBAC engine
# ──────────────────────────────────────────────────────────────────────────────

class CBAC:
    """
    CBAC engine — deploys cards, fetches them from Rubix, and verifies
    chains against policy.

    Lives separately from ``core.AgentDNA``: ``core.py`` soft-imports
    this module only when ``cbac=True`` was set on the AgentDNA
    instance.
    """

    def __init__(self, trust: RubixTrustService) -> None:
        self.trust = trust
        self._card_cache: Dict[str, Card] = {}

    # ─── fetch + verify a card from Rubix ─────────────────────────────────
    
    def _message(self, resp: requests.Response) -> str:
        try:
            b = resp.json()
            return (b.get("data") or {}).get("message") or b.get("message") or resp.text
        except ValueError:
            return resp.text
    
    def authorize_action(
        self,
        agent_id: str,
        action_intent: str,
        *,
        url: str,
        method: str = "POST",
        headers: dict | None = None,
        body: str | dict | None = None,
        chain_url: str = "http://localhost:8080",
        timeout: float = 35.0,
    ) -> requests.Response:
        if isinstance(body, dict):
            body = json.dumps(body)
            headers = {"Content-Type": "application/json", **(headers or {})}

        payload = {
            "agent_id": agent_id,
            "action_intent": action_intent,
            "app_request": {
                "url": url,
                "method": method,
                "headers": headers or {},
                "body": body or "",
            },
        }

        resp = requests.post(
            f"{chain_url.rstrip('/')}/authorize-action",
            json=payload,
            timeout=timeout,
        )

        decision = resp.headers.get("X-CBAC-Decision")

        if decision == "deny":
            raise PermissionError(self._message(resp))
        if decision == "error":
            raise RuntimeError(self._message(resp))

        return resp

    def fetch_card(self, nft_address: str) -> Card:
        """Pull a card NFT from Rubix and parse it. Cached after first hit."""
        if nft_address in self._card_cache:
            return self._card_cache[nft_address]

        # Lazy import — chain query dep is optional at import time
        from rubix.client import RubixClient
        from rubix.querier import Querier

        client = RubixClient(node_url=self.trust.base_url, timeout=300)
        states = Querier(client).get_nft_states(
            nft_address=nft_address,
            only_latest_state=True,
        )
        if isinstance(states, dict):
            states = [states]
        if not states:
            raise RuntimeError(f"No NFT state found for card {nft_address}")

        skill_md_text = _extract_skill_md(states[0])
        if skill_md_text is None:
            raise RuntimeError(f"Card NFT {nft_address} has no skill_md field")

        card = parse_skill_md(skill_md_text)
        card.nft_address = nft_address
        self._card_cache[nft_address] = card
        return card

    # ─── main entry point ─────────────────────────────────────────────────
    async def _get_policy(self, agent_id: str) -> Optional[str]:
        """
        Fetch the agent's policy from the Provenance Layer.

        Reads the latest agent-NFT state for ``agent_id``. The state's
        ``data`` is a JSON string of the form::

            {"type": "agent_nft", "agent_did": "...",
             "agent_metadata": {...}, "policy": "<base64>"}

        We parse it, pull the base64-encoded ``policy``, and decode it to the
        raw policy text. :meth:`verify_async` is format-agnostic and scores
        whatever text comes back. Returns ``None`` when no policy is present.
        """
        # Lazy import — chain query dep is optional at import time
        from rubix.client import RubixClient
        from rubix.querier import Querier

        client = RubixClient(node_url=self.trust.base_url, timeout=300)
        # get_nft_states is a blocking network call — offload it so it doesn't
        # stall the event loop when verify_async is awaited from a server.
        agent_log = await asyncio.to_thread(
            Querier(client).get_nft_states,
            nft_address=agent_id,
            only_latest_state=True,
        )

        # only_latest_state can return a bare dict instead of a list.
        if isinstance(agent_log, dict):
            agent_log = [agent_log]
        if not agent_log:
            raise RuntimeError(f"No NFT state found for card {agent_id}")

        latest_agent_log = agent_log[-1]["data"]

        # `data` is a JSON string — parse it to a dict.
        record = json.loads(latest_agent_log)

        # `policy` is base64-encoded policy text.
        policy_b64 = record.get("policy")
        if not policy_b64:
            return None

        return base64.b64decode(policy_b64).decode("utf-8")

    async def verify_async(self, agent_id: str, intended_action: Any) -> CBACResult:
        """
        Validate an agent's *intent* against the free-form content of its
        policy, using deterministic lexical semantics (no LLM).

        The policy is treated as an arbitrary blob — no fixed file format is
        assumed. Whatever :meth:`_get_policy` returns (raw text, dict, or a
        :class:`Card`) is flattened to text and scored. If the policy happens
        to carry structured allow/forbid lists, those are used as an extra
        signal; otherwise the whole-text overlap drives the decision.

        Parameters
        ----------
        agent_id:
            Used to fetch the agent's policy via :meth:`_get_policy`.
        intended_action:
            The action the agent wants to perform. A dict (or any object with
            these attrs) is understood; recognised keys:
              - ``action``      — the verb/identifier, e.g. ``"book_flight"``
              - ``description`` / ``summary`` — free-text description
              - ``params`` / ``args`` — a dict of arguments
            A plain string is also accepted and treated as the description.

        Returns
        -------
        CBACResult with ``decision`` in ``{"allow", "deny"}`` and a
        human-readable ``reason`` listing the matched / missing policy terms.
        The gate is fail-closed: any uncertainty (no policy, empty content,
        weak overlap) resolves to ``deny``.
        """
        # Fail-closed: any error fetching/decoding the policy denies.
        try:
            policy = await self._get_policy(agent_id)
        except Exception as e:
            return CBACResult(
                decision="deny",
                reason=f"Policy lookup failed for agent {agent_id}: {e}",
            )
        if not policy:
            return CBACResult(
                decision="deny",
                reason=f"No policy available for agent {agent_id}; cannot verify intent",
            )

        # 1. Flatten the policy — whatever its shape — into one vocabulary.
        #    allowed/forbidden lists are best-effort: present only if the
        #    policy carries that structure, never required.
        allowed_vocab, forbidden_vocab = _policy_signal_vocabs(policy)
        policy_vocab = _normalize_tokens(_collect_text(policy))

        if not policy_vocab:
            return CBACResult(
                decision="deny",
                reason=f"Policy for agent {agent_id} carries no analysable content",
            )

        # 2. Tokenize the intended action.
        action_tokens = _normalize_tokens(_intended_action_text(intended_action))
        if not action_tokens:
            return CBACResult(
                decision="deny",
                reason="Intended action carries no analysable content",
            )

        # 3. Score: coverage = fraction of the intent's words the policy knows.
        matched = action_tokens & policy_vocab
        missing = action_tokens - policy_vocab
        hit_forbidden = action_tokens & forbidden_vocab
        coverage = len(matched) / len(action_tokens)
        verb_hit = bool(action_tokens & allowed_vocab)

        # 4. Map to a binary decision (fail-closed). A verb hit — the action's
        #    own verb appears in the policy's allow-list — is a strong enough
        #    signal to allow on its own; otherwise we require the intent to be
        #    at least half grounded in the policy vocabulary.
        if hit_forbidden:
            return CBACResult(decision="deny", reason=(
                f"Intent overlaps forbidden policy terms {sorted(hit_forbidden)}"
            ))
        if verb_hit or coverage >= 0.5:
            return CBACResult(decision="allow", reason=(
                f"Intent grounded in policy (coverage={coverage:.0%}, "
                f"matched={sorted(matched)})"
            ))
        return CBACResult(decision="deny", reason=(
            f"Intent not grounded in policy (coverage={coverage:.0%}, "
            f"missing={sorted(missing)})"
        ))

    async def verify(self, ctx) -> CBACResult:
        """
        Run CBAC checks against a verified inbound ``RequestContext``.

        Walks the delegation chain inside ``ctx.host_block``, fetches
        each layer's card NFT, and runs deterministic policy checks.
        Returns a structured ``CBACResult`` with per-layer trace.
        """
        # Lazy import to avoid circular reference at module load.
        from .core import AgentDNA

        host_block = ctx.host_block if ctx is not None else None
        if not isinstance(host_block, dict):
            return CBACResult(decision="deny", reason="No host block in context")

        chain = AgentDNA._walk_chain(host_block)
        # chain[0]  = outermost sender; chain[-1] = root user
        # Skip the root user — users have intents, not action cards.
        layers_to_check = chain[:-1] if len(chain) > 1 else chain

        trace: List[CardCheck] = []
        any_deny = False

        # For each layer we need the "next hop" — the layer above it — to
        # check `can-delegate-to`. For the outermost layer the next hop is
        # the resource itself (whoever owns this CBAC engine), so we
        # synthesise a stand-in {agent: <resource DID>} for that check.
        resource_did = getattr(self.trust, "did", None)
        for i, block in enumerate(layers_to_check):
            if i > 0:
                next_block = layers_to_check[i - 1]
            elif resource_did:
                next_block = {"agent": resource_did}
            else:
                next_block = None
            check = await self._check_layer(block, next_block, ctx=ctx)
            trace.append(check)
            if not check.passed:
                any_deny = True

        if any_deny:
            failed = [c.layer_did for c in trace if not c.passed]
            return CBACResult(
                decision="deny",
                reason=f"Policy violations at: {', '.join(failed)}",
                trace=trace,
            )

        return CBACResult(decision="allow", reason="All cards permit the chain", trace=trace)

    # ─── per-layer checks ─────────────────────────────────────────────────

    async def _check_layer(
        self,
        block: Dict[str, Any],
        next_block: Optional[Dict[str, Any]],
        *,
        ctx,
    ) -> CardCheck:
        layer_did = block.get("agent")
        env = block.get("envelope") or {}
        card_nft = env.get("agent_card_nft")

        reasons: List[str] = []

        # No card attached → deny (configurable in future; v1 is strict).
        if not card_nft:
            return CardCheck(
                layer_did=layer_did or "<unknown>",
                card_nft=None,
                card=None,
                action=None,
                passed=False,
                reasons=["No agent_card_nft attached to envelope"],
            )

        # Fetch the card.
        try:
            card = self.fetch_card(card_nft)
        except Exception as e:
            return CardCheck(
                layer_did=layer_did or "<unknown>",
                card_nft=card_nft,
                card=None,
                action=None,
                passed=False,
                reasons=[f"Card fetch failed: {e}"],
            )

        # Extract the action this layer is performing.
        orig_msg = (
            (env.get("payload") or {}).get("original_message")
            or (env.get("payload") or {}).get("message")
            or env.get("original_message")
        )
        action = _extract_action(orig_msg)

        # ─── deterministic checks ───
        # 1. Card binds to this agent's DID.
        if card.agent_did != layer_did:
            reasons.append(
                f"Card agent-did={card.agent_did} does not match envelope signer {layer_did}"
            )
        # 2. Card not expired.
        now = datetime.now(timezone.utc)
        if card.expires_at < now:
            reasons.append(f"Card expired at {card.expires_at.isoformat()}")
        # 3. Action is in allowed-actions.
        if action is None:
            reasons.append("No action field in envelope's original_message")
        else:
            if action in card.forbidden_actions:
                reasons.append(f"Action {action!r} is in forbidden-actions")
            elif action not in card.allowed_actions:
                reasons.append(
                    f"Action {action!r} is not in allowed-actions "
                    f"{card.allowed_actions}"
                )
        # 4. Constraints (numeric / string-list).
        constraint_failures = _check_constraints(orig_msg, card.constraints)
        reasons.extend(constraint_failures)
        # 5. Delegation: if there's a layer above us, our card must allow that DID.
        if next_block is not None and card.can_delegate_to:
            next_did = next_block.get("agent")
            if next_did and next_did not in card.can_delegate_to:
                reasons.append(
                    f"Delegation to {next_did} not permitted by can-delegate-to {card.can_delegate_to}"
                )
        # 6. `requires` preconditions — currently only `user-intent-contains`.
        req_failures = _check_requires(card.requires, ctx)
        reasons.extend(req_failures)

        return CardCheck(
            layer_did=layer_did or "<unknown>",
            card_nft=card_nft,
            card=card,
            action=action,
            passed=not reasons,
            reasons=reasons,
        )


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

# Lexical-semantics helpers for verify_async (deterministic intent matching).

# Small stopword set — these words carry no policy signal so we drop them
# before scoring overlap.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for", "from",
    "i", "in", "is", "it", "its", "me", "my", "of", "on", "or", "that", "the",
    "this", "to", "want", "wants", "with", "would", "you", "your", "please",
})

# Tiny hand-curated synonym map: each token is folded to a canonical form so
# policy and intent vocabularies meet in the middle. Pure data, no model.
_SYNONYMS = {
    "buy": "purchase", "purchasing": "purchase", "purchases": "purchase",
    "book": "reserve", "booking": "reserve", "reservation": "reserve",
    "send": "transfer", "sending": "transfer", "pay": "transfer",
    "payment": "transfer", "remit": "transfer",
    "fetch": "read", "get": "read", "retrieve": "read", "query": "read",
    "write": "update", "edit": "update", "modify": "update", "change": "update",
    "remove": "delete", "drop": "delete",
}


def _stem(word: str) -> str:
    """Very light suffix stripping so book/books/booking collapse together."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _normalize_tokens(text: str) -> set:
    """Lowercase, split on non-alphanumerics, drop stopwords, fold synonyms,
    then stem — yielding a comparable bag of policy/intent terms."""
    out: set = set()
    for raw in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if not raw or raw in _STOPWORDS:
            continue
        token = _SYNONYMS.get(raw, raw)
        out.add(_stem(token))
    return out


def _collect_text(obj: Any, *, include_keys: bool = True, _depth: int = 0) -> str:
    """Recursively gather every string found in an arbitrary blob.

    Format-agnostic: handles raw text, dicts, lists, and dataclasses/objects
    (e.g. :class:`Card`) alike.

    ``include_keys`` controls whether mapping *keys* are gathered alongside
    values. For a large policy blob, keys are useful searchable vocabulary
    (default). For a short intent, structural keys like ``action``/``params``
    are pure scaffolding that would dilute the coverage score, so the intent
    side passes ``include_keys=False`` to flatten values only.
    """
    if _depth > 6 or obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, dict):
        parts: List[str] = []
        for k, v in obj.items():
            if include_keys:
                parts.append(str(k))
            parts.append(_collect_text(v, include_keys=include_keys, _depth=_depth + 1))
        return " ".join(parts)
    if isinstance(obj, (list, tuple, set)):
        return " ".join(
            _collect_text(v, include_keys=include_keys, _depth=_depth + 1) for v in obj
        )
    # Dataclass / arbitrary object → walk its __dict__.
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict):
        return _collect_text(data, include_keys=include_keys, _depth=_depth + 1)
    return str(obj)


# Keys under which a policy *might* expose structured allow / forbid lists.
# Purely best-effort — absence is fine; whole-text overlap still drives the
# decision. Matched case-insensitively as substrings of the key.
_ALLOW_KEY_HINTS = ("allow", "permit", "grant", "can", "capabilit", "scope")
_FORBID_KEY_HINTS = ("forbid", "deny", "prohibit", "disallow", "block", "restrict")


def _policy_signal_vocabs(policy: Any) -> tuple:
    """Best-effort extraction of (allowed_vocab, forbidden_vocab) from a policy
    of unknown shape. Returns empty sets when no such structure is present."""
    allowed: set = set()
    forbidden: set = set()

    # Parsed Card has dedicated fields.
    if isinstance(policy, Card):
        allowed |= _normalize_tokens(" ".join(policy.allowed_actions))
        forbidden |= _normalize_tokens(" ".join(policy.forbidden_actions))
        return allowed, forbidden

    # JSON-string policy → parse then recurse.
    if isinstance(policy, str):
        stripped = policy.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return _policy_signal_vocabs(json.loads(policy))
            except (TypeError, json.JSONDecodeError):
                return allowed, forbidden
        return allowed, forbidden

    # Dict-shaped policy → scan keys for allow/forbid hints, at any depth.
    if isinstance(policy, dict):
        for k, v in policy.items():
            key = str(k).lower()
            if any(h in key for h in _FORBID_KEY_HINTS):
                forbidden |= _normalize_tokens(_collect_text(v))
            elif any(h in key for h in _ALLOW_KEY_HINTS):
                allowed |= _normalize_tokens(_collect_text(v))
            elif isinstance(v, dict):
                a, f = _policy_signal_vocabs(v)
                allowed |= a
                forbidden |= f

    return allowed, forbidden


def _intended_action_text(intended_action: Any) -> str:
    """Flatten an intended-action of *any* shape (str / dict / list / object)
    into one string for tokenizing.

    Format-agnostic by design — it reuses the same recursive flattener as the
    policy side (:func:`_collect_text`), so no fixed key schema is assumed.
    Common keys like ``action``/``description``/``params`` are picked up
    automatically because their string values are gathered. Field *names* are
    skipped (``include_keys=False``) — for a short intent they are structural
    scaffolding that would only dilute the coverage score.
    """
    return _collect_text(intended_action, include_keys=False)


def _extract_action(original_message: Any) -> Optional[str]:
    """Pull the ``action`` field out of an envelope's original_message."""
    if isinstance(original_message, str):
        try:
            obj = json.loads(original_message)
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(obj, dict):
            return obj.get("action")
        return None
    if isinstance(original_message, dict):
        return original_message.get("action")
    return None


def _payload_dict(original_message: Any) -> Dict[str, Any]:
    if isinstance(original_message, dict):
        return original_message
    if isinstance(original_message, str):
        try:
            obj = json.loads(original_message)
        except (TypeError, json.JSONDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


def _check_constraints(original_message: Any, constraints: Dict[str, Any]) -> List[str]:
    """
    v1 constraint grammar:
        max-<key>     : payload value must be <= constraint value
        min-<key>     : payload value must be >= constraint value
        allowed-<key> : payload value must be in the list (or '*' wildcard allows all)
    """
    failures: List[str] = []
    payload = _payload_dict(original_message)

    for key, limit in constraints.items():
        if key.startswith("max-"):
            target = key[len("max-"):]
            val = payload.get(target)
            if val is None:
                continue
            try:
                if float(val) > float(limit):
                    failures.append(f"{target}={val} exceeds max {limit}")
            except (TypeError, ValueError):
                failures.append(f"{target}={val!r} not numeric for max check")
        elif key.startswith("min-"):
            target = key[len("min-"):]
            val = payload.get(target)
            if val is None:
                continue
            try:
                if float(val) < float(limit):
                    failures.append(f"{target}={val} below min {limit}")
            except (TypeError, ValueError):
                failures.append(f"{target}={val!r} not numeric for min check")
        elif key.startswith("allowed-"):
            target = key[len("allowed-"):]
            val = payload.get(target)
            if val is None:
                continue
            allowed = limit if isinstance(limit, list) else [limit]
            if "*" in allowed:
                continue
            if val not in allowed:
                failures.append(f"{target}={val!r} not in allowed list {allowed}")
        # Unknown prefix — silently ignore so admins can extend later
        # without breaking older verifiers.

    return failures


def _check_requires(requires: Dict[str, Any], ctx) -> List[str]:
    """
    v1 `requires` grammar:
        user-intent-contains: [word, ...] — root user's intent text must
            contain at least one of these keywords (case-insensitive).
    """
    failures: List[str] = []

    needles = requires.get("user-intent-contains")
    if needles:
        intent = (ctx.user_intent or "").lower() if ctx is not None else ""
        needles_list = needles if isinstance(needles, list) else [needles]
        if not any(str(n).lower() in intent for n in needles_list):
            failures.append(
                f"user-intent {intent!r} does not contain any of {needles_list}"
            )

    return failures


def _extract_skill_md(state: Dict[str, Any]) -> Optional[str]:
    """Recursively search an NFT state dict for the skill_md field.
    Also handles identity NFT format where policy is base64-encoded skill.md."""
    if isinstance(state, dict):
        if "skill_md" in state and isinstance(state["skill_md"], str):
            return state["skill_md"]
        # Identity NFT format: policy is base64-encoded skill.md content.
        if "policy" in state and isinstance(state["policy"], str) and state.get("type") == "agent_nft":
            try:
                decoded = base64.b64decode(state["policy"]).decode("utf-8")
                if decoded.strip().startswith("---"):
                    return decoded
            except Exception:
                pass
        for v in state.values():
            if isinstance(v, str):
                stripped = v.lstrip()
                if stripped.startswith(("{", "[")):
                    try:
                        parsed = json.loads(v)
                        found = _extract_skill_md(parsed)
                        if found:
                            return found
                    except (TypeError, json.JSONDecodeError):
                        pass
            elif isinstance(v, (dict, list)):
                found = _extract_skill_md(v)
                if found:
                    return found
    elif isinstance(state, list):
        for item in state:
            found = _extract_skill_md(item)
            if found:
                return found
    return None
