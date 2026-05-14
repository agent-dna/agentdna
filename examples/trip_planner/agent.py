"""
Trip-planner pipeline — depth-3 Chain of Custody, in-process.

    Traveller ──intent──▶ Concierge ──task──▶ FlightAgent ──leg──▶ BookingAgent
              (root user, owns NFT)                                   (resource)

Each hop wraps the previous signer's full signed block via ``parent=``.
After BookingAgent replies, the Traveller verifies + writes the audit-log
record to the Rubix chain.

Exposed for the Streamlit app: ``TripPlanner.plan(intent)`` runs the whole
chain and returns a dict the UI can render; ``history()`` fetches the
on-chain audit records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agentdna import AgentDNA


# ── Deterministic mock work each "expert" agent does on its inbound task ──
# Real apps would call an LLM / external API here. For this demo the focus
# is the chain itself, so each agent returns a small structured payload.
# Payloads stay short on purpose: every agent's content is duplicated at
# each layer of the nested chain (paper-spec recursive nesting) AND again
# in the on-chain audit record, so verbose payloads quickly exceed the
# Rubix node's per-tx size limit.

def _concierge_plan() -> Dict[str, Any]:
    # Don't echo the upstream intent — it's already in our parent_block
    # (the traveller's signed envelope). Just record what we decided to do.
    return {
        "legs":      ["flight", "hotel", "return"],
        "next_step": "delegate flight",
    }


def _flight_search() -> Dict[str, Any]:
    # No echo of upstream context — it's already in our parent_block.
    return {
        "route":     "SFO->NRT",
        "carrier":   "JAL",
        "max_price": 1200,
        "criteria":  "non-stop economy",
    }


def _booking_confirm() -> Dict[str, Any]:
    return {
        "flight":    "JL001",
        "route":     "SFO->NRT",
        "departure": "2026-06-12 10:55",
        "arrival":   "2026-06-13 14:30",
        "fare_usd":  1150,
        "pnr":       "7K3X9Q",
    }


class TripPlanner:
    """
    Holds the four AgentDNA identities and runs one signed turn through
    the chain. The Traveller is the only chain writer (enable_nft=True).
    """

    def __init__(self, *, user_alias: str, api_key: str) -> None:
        self.user_alias = user_alias
        self.traveller = AgentDNA(alias=user_alias, api_key=api_key)
        self.concierge = AgentDNA(alias="ConciergeAgent", api_key=api_key, enable_nft=False)
        self.flight    = AgentDNA(alias="FlightAgent",    api_key=api_key, enable_nft=False)
        self.booking   = AgentDNA(alias="BookingAgent",   api_key=api_key, enable_nft=False)

        self.last_nft_result: Optional[Dict[str, Any]] = None
        self.last_chain_depth: int = 0

    # ── identifiers / chain queries ────────────────────────────────────────

    @property
    def nft_token(self) -> Optional[str]:
        return self.traveller.nft_token

    def history(self, latest: bool = False) -> List[Dict[str, Any]]:
        return self.traveller.history(latest=latest)

    # ── one signed pass through the four-party chain ───────────────────────

    def plan(self, intent: str) -> Dict[str, Any]:
        """
        Run one turn: traveller → concierge → flight → booking, then verify
        + audit on the way back. Returns a dict the UI renders.
        """
        return asyncio.run(self._plan(intent))

    async def _plan(self, intent: str) -> Dict[str, Any]:
        # ── Hop 1: traveller signs intent ─────────────────────────────────
        traveller_signed = self.traveller.build({
            "intent": intent,
            "ts":     datetime.now(timezone.utc).isoformat(),
        })

        # ── Hop 2: concierge wraps the traveller's block, dispatches ──────
        concierge_payload = _concierge_plan()
        env_concierge = self.concierge.build(concierge_payload, parent=traveller_signed)

        # ── Hop 3: flight agent verifies inbound, then forwards to booking
        #          by passing its RequestContext as parent (auto-wraps).
        ctx_flight = await self.flight.handle(str(env_concierge), verify_mode="heavy")
        if not ctx_flight.verified:
            raise RuntimeError(f"FlightAgent failed to verify Concierge: {ctx_flight.trust_issues}")

        flight_payload = _flight_search()
        env_flight = self.flight.build(flight_payload, parent=ctx_flight)

        # ── Hop 4: booking agent verifies the 3-layer chain in heavy mode ─
        ctx_booking = await self.booking.handle(str(env_flight), verify_mode="heavy")
        if not ctx_booking.verified:
            raise RuntimeError(f"BookingAgent failed to verify chain: {ctx_booking.trust_issues}")

        # ── Booking signs the confirmation reply under the verified ctx ──
        booking_payload = _booking_confirm()
        reply = self.booking.build(booking_payload, ctx=ctx_booking)

        # ── Traveller verifies the reply + writes the audit-log NFT ───────
        result = await self.traveller.handle(
            reply,
            original=env_flight,
            remote_name="BookingAgent",
            execute_nft=True,
        )
        self.last_nft_result = result.nft_result
        self.last_chain_depth = len(AgentDNA._walk_chain(env_flight.host_block))

        return {
            "intent":             intent,
            "chain_depth":        self.last_chain_depth,
            "verified":           result.verified,
            "user_verified":      result.user_verified,
            "trust_issues":       result.trust_issues,
            "traveller_did":      self.traveller.did,
            "concierge_did":      self.concierge.did,
            "flight_did":         self.flight.did,
            "booking_did":        self.booking.did,
            "concierge_payload":  concierge_payload,
            "flight_payload":     flight_payload,
            "booking_payload":    booking_payload,
            "nft_result":         result.nft_result,
        }
