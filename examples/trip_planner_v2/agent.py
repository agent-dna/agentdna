"""
Trip-planner v2 — same scenario as examples/trip_planner, rewritten on
the new AgentDNA APIs.

What's new compared to v1
-------------------------

  v1                                          v2
  ──                                          ──
  AgentDNA(alias=...)                         AgentDNA(alias=..., kind="user",
                                                       metadata={...})
  AgentDNA(alias=...)                         AgentDNA(alias=..., kind="agent",
                                                       policy_file="skills/<name>.md")
  CBAC(trust=admin.trust).deploy_card(admin,  agent.deploy_card()
   skill_path)                                  → skill.md base64 → `policy`
                                                  field of identity NFT
  user_signed = user.build({"intent": ...})   user_env, user_ctx = await
  ctx = await user.handle(user_signed)         user.initialise_intent({"intent": ...})

Defence layers (unchanged in shape):
  1. Self-check     — agent reads its own card and refuses upfront if the
                      intent / action doesn't match.
  2. Upstream-check — receiving agent verifies upstream's card.
  3. CBAC at resource — BookingAgent's handle(cbac=True) runs the full
                      policy walk. (Disabled by default in v2; CBAC engine
                      is being updated to fetch policy from the new
                      identity-NFT shape — flip ``enable_cbac=True`` once
                      the engine lands.)

Chain
-----

  Traveller (user) -> ConciergeAgent -> FlightAgent -> BookingAgent
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

from agentdna import AgentDNA, Card, parse_skill_md


HERE = Path(__file__).parent
SKILLS_DIR = HERE / "skills"


# ── Mock payloads ──────────────────────────────────────────────────────────

def _concierge_plan() -> Dict[str, Any]:
    return {
        "action":    "delegate_trip_to_flight",
        "legs":      ["flight", "hotel", "return"],
        "next_step": "delegate flight",
    }


def _concierge_rogue() -> Dict[str, Any]:
    # Forbidden action listed in concierge.md's forbidden-actions.
    return {"action": "book_flight", "rogue": True}


def _flight_search() -> Dict[str, Any]:
    return {
        "action":    "request_flight_booking",
        "route":     "SFO-NRT",
        "carrier":   "JAL",
        "max_price": 1200,
        "criteria":  "non-stop economy",
    }


def _flight_search_breach() -> Dict[str, Any]:
    # max_price exceeds the card's max-max_price: 2000 constraint.
    return {
        "action":    "request_flight_booking",
        "route":     "SFO-NRT",
        "carrier":   "JAL",
        "max_price": 5000,
        "criteria":  "luxury",
    }


def _booking_confirm() -> Dict[str, Any]:
    return {
        "flight":    "JL001",
        "route":     "SFO-NRT",
        "departure": "2026-06-12 10:55",
        "arrival":   "2026-06-13 14:30",
        "fare_usd":  1150,
        "pnr":       "7K3X9Q",
    }


# ── Skill.md rendering + self-issuance ────────────────────────────────────

def _render_skill(skill_path: Path, **substitutions: str) -> str:
    return Template(skill_path.read_text(encoding="utf-8")).safe_substitute(**substitutions)


def _self_issue_card(dna: AgentDNA, rendered_text: str) -> Tuple[str, Card]:
    """
    Self-issue a policy card.

    The agent encodes its rendered skill.md, stamps it as ``policy_encoded``,
    and publishes the identity NFT. The NFT's ``policy`` field carries the
    base64; the NFT address itself becomes the agent's ``card_nft`` so every
    envelope this agent signs attaches it.

    Pre-requisite: the rendered card's ``issued-by`` must equal ``dna.did``
    (this is what "self-issued" means — issuer and holder are the same).
    Project-level validation only — AgentDNA itself treats the policy file
    as opaque bytes.
    """
    card = parse_skill_md(rendered_text)
    if card.issued_by != dna.did:
        raise ValueError(
            f"Skill card issued-by={card.issued_by!r} does not equal "
            f"self.did={dna.did!r}. Self-issued cards must match."
        )
    dna.policy = base64.b64encode(rendered_text.encode("utf-8")).decode("ascii")
    nft_address = dna.deploy_agent_card()
    dna.card_nft = nft_address
    return nft_address, card


# ── Self-check (layer 1) + upstream-check (layer 2) ────────────────────────

def _intent_ok(card: Card, intent: str) -> Tuple[bool, str]:
    needles = card.requires.get("user-intent-contains") or []
    if not needles:
        return True, ""
    intent_l = (intent or "").lower()
    if any(str(n).lower() in intent_l for n in needles):
        return True, ""
    return False, f"intent does not mention any of {needles}"


def _action_ok(card: Card, action: Optional[str]) -> Tuple[bool, str]:
    if action is None:
        return False, "no action field in payload"
    if action in card.forbidden_actions:
        return False, f"action {action!r} is forbidden"
    if action not in card.allowed_actions:
        return False, f"action {action!r} not in allowed-actions"
    return True, ""


def _upstream_ok(upstream_card: Card, my_did: str, upstream_action: Optional[str]) -> Tuple[bool, str]:
    """Receiving agent's quick check against the immediate upstream's card."""
    ok, why = _action_ok(upstream_card, upstream_action)
    if not ok:
        return False, f"upstream tried bad action: {why}"
    if upstream_card.can_delegate_to and my_did not in upstream_card.can_delegate_to:
        return False, "upstream's can-delegate-to does not include me"
    return True, ""


# ── TripPlanner ────────────────────────────────────────────────────────────

class TripPlanner:
    def __init__(
        self,
        *,
        user_alias: str,
        api_key: str,
        user_metadata: Optional[Dict[str, Any]] = None,
        enable_cbac: bool = False,
    ) -> None:
        self.user_alias = user_alias
        self.enable_cbac = enable_cbac

        # Traveller — kind="user" with metadata stamped on the identity NFT.
        # Use a sensible default for the demo; callers can override.
        self.traveller = AgentDNA(
            alias=user_alias,
            api_key=api_key,
            kind="user",
            metadata=user_metadata or {
                "org":        "ACME Travel",
                "membership": "platinum",
                "role":       "trip_planner",
            },
        )

        # Agents — construct with enable_nft=False first so their DIDs are
        # known. We need the DIDs to render each skill.md template (issued-by
        # = self DID), then call deploy_card() to publish the identity NFT
        # with the rendered policy embedded.
        self.concierge = AgentDNA(alias="ConciergeAgent", api_key=api_key,
                                  kind="agent", enable_nft=False)
        self.flight    = AgentDNA(alias="FlightAgent",    api_key=api_key,
                                  kind="agent", enable_nft=False)
        self.booking   = AgentDNA(alias="BookingAgent",   api_key=api_key,
                                  kind="agent", enable_nft=False,
                                  cbac=enable_cbac)

        self.concierge_card: Optional[Card] = None
        self.flight_card:    Optional[Card] = None
        self.last_nft_result: Optional[Dict[str, Any]] = None
        self.last_chain_depth: int = 0

        self._provision_self_issued_cards()

    def _provision_self_issued_cards(self) -> None:
        """
        Render each agent's skill.md template and self-issue it as a policy.
        """
        issued_at  = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=365)).replace(
            microsecond=0).isoformat()

        common = {
            "ISSUED_AT":   issued_at,
            "EXPIRES_AT":  expires_at,
            "FLIGHT_DID":  self.flight.did,
            "BOOKING_DID": self.booking.did,
        }

        concierge_text = _render_skill(
            SKILLS_DIR / "concierge.md",
            SELF_DID=self.concierge.did,
            **common,
        )
        flight_text = _render_skill(
            SKILLS_DIR / "flight.md",
            SELF_DID=self.flight.did,
            **common,
        )

        _, self.concierge_card = _self_issue_card(self.concierge, concierge_text)
        _, self.flight_card    = _self_issue_card(self.flight,    flight_text)

    # ── Public surface ────────────────────────────────────────────────────

    @property
    def nft_token(self) -> Optional[str]:
        return self.traveller.nft_token

    def history(self, latest: bool = False) -> List[Dict[str, Any]]:
        return self.traveller.history(latest=latest)

    def plan(
        self,
        intent: str,
        *,
        rogue_concierge: bool = False,
        wrong_delegation: bool = False,
        constraint_breach: bool = False,
    ) -> Dict[str, Any]:
        return asyncio.run(self._plan(
            intent,
            rogue_concierge=rogue_concierge,
            wrong_delegation=wrong_delegation,
            constraint_breach=constraint_breach,
        ))

    # ── Chain orchestration ───────────────────────────────────────────────

    async def _plan(
        self,
        intent: str,
        *,
        rogue_concierge: bool,
        wrong_delegation: bool,
        constraint_breach: bool,
    ) -> Dict[str, Any]:
        base = self._base_result(intent, rogue_concierge, wrong_delegation, constraint_breach)

        # ── Hop 1: traveller initialises intent ───────────────────────────
        # build() + handle() folded into one await. traveller_signed is the
        # SignedEnvelope; traveller_ctx is the self-verified RequestContext.
        active_toggles = []
        if rogue_concierge:   active_toggles.append("rogue_concierge")
        if wrong_delegation:  active_toggles.append("wrong_delegation")
        if constraint_breach: active_toggles.append("constraint_breach")

        intent_envelope: Dict[str, Any] = {
            "intent": intent,
            "ts":     datetime.now(timezone.utc).isoformat(),
        }
        if active_toggles:
            intent_envelope["sim"] = active_toggles

        traveller_signed, traveller_ctx = await self.traveller.initialise_intent(intent_envelope)
        if not traveller_ctx.verified:
            raise RuntimeError(
                f"Traveller self-verification failed: {traveller_ctx.trust_issues}"
            )

        # ── Layer 1: Concierge self-check ─────────────────────────────────
        # Skipped under rogue_concierge so layer 2 catches it instead.
        if not rogue_concierge and self.concierge_card is not None:
            ok, why = _intent_ok(self.concierge_card, intent)
            if not ok:
                return {
                    **base,
                    "caught_at":      "layer-1 self-check",
                    "caught_by":      "ConciergeAgent",
                    "caught_why":     why,
                    "verified":       False,
                    "refused":        True,
                    "refusal_reason": f"ConciergeAgent declined: {why}",
                    "booking_payload": {
                        "refused_by": "ConciergeAgent", "layer": 1, "why": why,
                    },
                    "nft_result":      None,
                    "chain_initiated": False,
                }

        # Concierge signs an outbound task — rogue path picks a forbidden action.
        concierge_payload = _concierge_rogue() if rogue_concierge else _concierge_plan()
        env_concierge = self.concierge.build(concierge_payload, parent=traveller_signed)

        # wrong_delegation: skip Flight, Concierge "delegates" straight to Booking
        if wrong_delegation:
            return await self._send_to_booking(
                env_for_booking=env_concierge,
                base=base,
                concierge_payload=concierge_payload,
                flight_payload=None,
                wrong_delegation=True,
            )

        # ── Hop 3: Flight verifies + layer-2 upstream-check ───────────────
        ctx_flight = await self.flight.handle(str(env_concierge), verify_mode="heavy")
        if not ctx_flight.verified:
            raise RuntimeError(f"FlightAgent CoCA failed: {ctx_flight.trust_issues}")

        if self.concierge_card is not None and self.flight_card is not None:
            up_ok, up_why = _upstream_ok(
                self.concierge_card,
                self.flight.did,
                concierge_payload.get("action"),
            )
            if not up_ok:
                refusal = {"refused": True, "by": "FlightAgent", "layer": 2, "why": up_why}
                reply = self.flight.build(refusal, ctx=ctx_flight)
                result = await self.traveller.handle(
                    reply,
                    original=env_concierge,
                    remote_name="FlightAgent",
                    execute_nft=True,
                )
                return {
                    **base,
                    "caught_at":       "layer-2 upstream-check",
                    "caught_by":       "FlightAgent",
                    "caught_why":      up_why,
                    "verified":        False,
                    "refused":         True,
                    "refusal_reason":  f"FlightAgent declined: {up_why}",
                    "concierge_payload": concierge_payload,
                    "booking_payload": refusal,
                    "nft_result":      result.nft_result,
                    "user_verified":   result.user_verified,
                    "trust_issues":    result.trust_issues,
                    "chain_initiated": True,
                }

        # Flight signs outbound — constraint_breach blows max_price past the cap.
        flight_payload = _flight_search_breach() if constraint_breach else _flight_search()
        env_flight = self.flight.build(flight_payload, parent=ctx_flight)

        return await self._send_to_booking(
            env_for_booking=env_flight,
            base=base,
            concierge_payload=concierge_payload,
            flight_payload=flight_payload,
            wrong_delegation=False,
        )

    async def _send_to_booking(
        self,
        *,
        env_for_booking,
        base: Dict[str, Any],
        concierge_payload: Dict[str, Any],
        flight_payload: Optional[Dict[str, Any]],
        wrong_delegation: bool,
    ) -> Dict[str, Any]:
        # Layer 3: BookingAgent. If enable_cbac=True the CBAC engine will run
        # inside handle(); otherwise we just do plain CoCA verification.
        ctx_booking = await self.booking.handle(str(env_for_booking), verify_mode="heavy")
        if not ctx_booking.verified:
            raise RuntimeError(f"BookingAgent CoCA failed: {ctx_booking.trust_issues}")

        cbac_result = ctx_booking.cbac_result
        cbac_trace = []
        if cbac_result is not None:
            for c in cbac_result.trace:
                cbac_trace.append({
                    "layer_did": c.layer_did,
                    "card_nft":  c.card_nft,
                    "card_name": c.card.agent_name if c.card else None,
                    "action":    c.action,
                    "passed":    c.passed,
                    "reasons":   c.reasons,
                })
        self.last_chain_depth = len(AgentDNA._walk_chain(env_for_booking.host_block))

        refused = (cbac_result is not None and cbac_result.decision == "deny")
        booking_payload = (
            {"refused": True, "n_denied": sum(1 for c in cbac_result.trace if not c.passed)}
            if refused
            else _booking_confirm()
        )

        reply = self.booking.build(booking_payload, ctx=ctx_booking)
        result = await self.traveller.handle(
            reply,
            original=env_for_booking,
            remote_name="BookingAgent",
            execute_nft=True,
        )
        self.last_nft_result = result.nft_result

        return {
            **base,
            "chain_depth":       self.last_chain_depth,
            "concierge_payload": concierge_payload,
            "flight_payload":    flight_payload,
            "verified":          result.verified and not refused,
            "user_verified":     result.user_verified,
            "trust_issues":      result.trust_issues,
            "booking_payload":   booking_payload,
            "refused":           refused,
            "refusal_reason":    cbac_result.reason if refused else None,
            "cbac_decision":     cbac_result.decision if cbac_result else None,
            "cbac_reason":       cbac_result.reason   if cbac_result else None,
            "cbac_trace":        cbac_trace,
            "caught_at":         "layer-3 cbac" if refused else None,
            "caught_by":         "BookingAgent (CBAC)" if refused else None,
            "caught_why":        cbac_result.reason if refused else None,
            "nft_result":        result.nft_result,
            "chain_initiated":   True,
            "wrong_delegation":  wrong_delegation,
        }

    def _base_result(
        self,
        intent: str,
        rogue_concierge: bool,
        wrong_delegation: bool,
        constraint_breach: bool,
    ) -> Dict[str, Any]:
        return {
            "intent":             intent,
            "chain_depth":        0,
            "traveller_did":      self.traveller.did,
            "concierge_did":      self.concierge.did,
            "flight_did":         self.flight.did,
            "booking_did":        self.booking.did,
            "concierge_card":     self.concierge.card_nft,
            "flight_card":        self.flight.card_nft,
            "concierge_payload":  None,
            "flight_payload":     None,
            "cbac_enabled":       self.enable_cbac,
            "cbac_decision":      None,
            "cbac_reason":        None,
            "cbac_trace":         [],
            "toggles": {
                "rogue_concierge":   rogue_concierge,
                "wrong_delegation":  wrong_delegation,
                "constraint_breach": constraint_breach,
            },
        }
