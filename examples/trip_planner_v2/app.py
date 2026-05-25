import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from agent import TripPlanner

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
DEFAULT_USER_ALIAS = "Traveller_USER"

st.set_page_config(page_title="Trip Planner", layout="wide")
st.title("Trip Planner")
st.caption(
    "Traveller -> ConciergeAgent -> FlightAgent -> BookingAgent  ·  "
    "AgentDNA recursive Chain of Custody (depth-3) + 3-layer CBAC defence"
)

if not AGENTDNA_API_KEY:
    st.error(
        "AGENTDNA_API_KEY not set. Copy .env.sample to .env and fill it in, "
        "then restart with ./run.sh"
    )
    st.stop()

if "history" not in st.session_state:
    st.session_state.history = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []
if "user_alias" not in st.session_state:
    st.session_state.user_alias = DEFAULT_USER_ALIAS

# ── Sidebar: user identity ────────────────────────────────────────────────────

st.sidebar.subheader("Signed in as")
new_alias = st.sidebar.text_input(
    "Traveller alias",
    value=st.session_state.user_alias,
    help="Your chain identity. Each unique alias gets its own DID + audit-log NFT.",
)

if new_alias != st.session_state.user_alias or "planner" not in st.session_state:
    st.session_state.user_alias = new_alias
    with st.spinner(f"Provisioning DID + NFT for {new_alias}..."):
        st.session_state.planner = TripPlanner(
            user_alias=new_alias,
            api_key=AGENTDNA_API_KEY,
        )

planner: TripPlanner = st.session_state.planner

st.sidebar.divider()

# ── Sidebar: defence-layer toggles ────────────────────────────────────────────

st.sidebar.subheader("Misbehaviour toggles")
st.sidebar.caption("Trigger each defence layer deliberately.")

rogue_concierge = st.sidebar.checkbox(
    "Rogue ConciergeAgent",
    value=False,
    help=(
        "ConciergeAgent skips its own self-check AND uses a forbidden action. "
        "Layer 2 (FlightAgent's upstream-check) should catch it."
    ),
)
wrong_delegation = st.sidebar.checkbox(
    "Wrong delegation",
    value=False,
    help=(
        "ConciergeAgent skips FlightAgent and tries to delegate straight to "
        "BookingAgent. ConciergeAgent's can-delegate-to does not include "
        "BookingAgent, so Layer 3 (CBAC) should deny."
    ),
)
constraint_breach = st.sidebar.checkbox(
    "Constraint breach",
    value=False,
    help=(
        "FlightAgent sends max_price = 5000, exceeding its card's "
        "max-max_price = 2000. Layer 3 (CBAC constraints check) should deny."
    ),
)

st.sidebar.divider()

# ── Sidebar: quick example trips ──────────────────────────────────────────────

EXAMPLE_TRIPS = [
    "Plan a 5-day trip to Tokyo for two adults in June.",
    "Book a long weekend in Lisbon for one traveller.",
    "order a pizza   (will be refused at layer 1 — intent doesn't match)",
    "Arrange a business trip: SFO to London Heathrow next Monday.",
]

st.sidebar.header("Quick Examples")
for q in EXAMPLE_TRIPS:
    if st.sidebar.button(q, use_container_width=True, key=f"ex_{q}"):
        st.session_state["intent_input"] = q.split("   (")[0]
        st.rerun()

st.sidebar.divider()

# ── Sidebar: NFT chain history ────────────────────────────────────────────────

st.sidebar.subheader("Chain History")

nft_token = planner.nft_token or ""
st.sidebar.caption(
    f"NFT: `{nft_token}`" if nft_token else "NFT: (will be deployed on first run)"
)

if st.sidebar.button("Fetch History Records", use_container_width=True, disabled=not nft_token):
    with st.spinner("Fetching NFT records from Rubix..."):
        st.session_state.chain_history = planner.history()
    st.rerun()

if st.sidebar.button("Clear Local History", use_container_width=True):
    st.session_state.history = []
    st.session_state.chain_history = []
    st.rerun()

if st.session_state.chain_history:
    with st.expander(
        f"Chain History — NFT `{nft_token}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.json(st.session_state.chain_history, expanded=2)

# ── Input ─────────────────────────────────────────────────────────────────────

intent = st.text_input(
    "Describe your trip",
    key="intent_input",
    placeholder="e.g. Plan a 5-day trip to Tokyo for two adults in June.",
)
plan_clicked = st.button("Plan trip", type="primary", disabled=not intent.strip())


# ── Render helpers ────────────────────────────────────────────────────────────

def _outcome_line(r: dict) -> str:
    refused = r.get("refused", False)
    caught_at = r.get("caught_at")
    if refused and caught_at:
        return f"refused at **{caught_at}**"
    if refused:
        return "refused"
    if r.get("verified"):
        return "verified and permitted"
    return "verification failed"


def _render_result(r: dict) -> None:
    cbac_decision = r.get("cbac_decision") or "n/a"
    chain = "yes" if r.get("chain_initiated") else "no (short-circuited)"

    st.markdown(
        f"**Outcome:** {_outcome_line(r)}  ·  "
        f"**Chain initiated:** {chain}  ·  "
        f"**Chain depth:** {r.get('chain_depth', 0)}  ·  "
        f"**CBAC:** {cbac_decision}"
    )

    if r.get("caught_by"):
        st.error(
            f"Caught by **{r['caught_by']}** at {r['caught_at']}.  \n"
            f"Reason: {r.get('caught_why') or r.get('refusal_reason')}"
        )

    if r.get("trust_issues"):
        st.warning("Trust issues: " + "; ".join(r["trust_issues"]))
    if r.get("cbac_setup_error"):
        st.warning(f"CBAC setup error: {r['cbac_setup_error']}")

    tabs = st.tabs([
        "Booking confirmation",
        "CBAC trace",
        "Concierge plan",
        "Flight search",
        "Audit (NFT write)",
        "Parties (DIDs + Cards)",
    ])

    with tabs[0]:
        st.subheader("BookingAgent — final reply")
        if r.get("refused"):
            st.error(f"Refused. {r.get('refusal_reason') or r.get('caught_why')}")
        st.json(r.get("booking_payload"), expanded=True)

    with tabs[1]:
        st.subheader("CBAC — per-layer policy check")
        if not r.get("cbac_enabled"):
            st.caption("CBAC is off for this run.")
        elif not r.get("cbac_trace"):
            st.caption("CBAC did not run (chain short-circuited earlier).")
        else:
            st.markdown(
                f"**Decision:** `{r['cbac_decision']}`  ·  "
                f"**Reason:** {r['cbac_reason']}"
            )
            for layer in r["cbac_trace"]:
                ok = layer["passed"]
                tag = "pass" if ok else "FAIL"
                with st.expander(
                    f"[{tag}]  {layer.get('card_name') or '<unnamed>'}  ·  "
                    f"action = `{layer.get('action')}`",
                    expanded=not ok,
                ):
                    st.markdown(f"**Signer DID:** `{layer['layer_did']}`")
                    st.markdown(f"**Card NFT:** `{layer['card_nft']}`")
                    if layer["reasons"]:
                        st.error("Policy violations:")
                        for reason in layer["reasons"]:
                            st.markdown(f"- {reason}")
                    else:
                        st.success("All checks passed.")

    with tabs[2]:
        st.subheader("ConciergeAgent — derived plan")
        st.json(r.get("concierge_payload"), expanded=True)

    with tabs[3]:
        st.subheader("FlightAgent — search criteria")
        st.json(r.get("flight_payload"), expanded=True)

    with tabs[4]:
        st.subheader("On-chain audit record")
        if not r.get("chain_initiated"):
            st.info(
                "No NFT was written. The agent declined upfront (layer 1 "
                "self-check), so no chain was ever initiated."
            )
        elif r.get("refused"):
            st.info(
                "BookingAgent signed a refusal rather than a booking. "
                "The traveller still wrote the audit record so the refused "
                "attempt is immutably recorded."
            )
        if r.get("nft_result"):
            st.json(r["nft_result"], expanded=True)
        else:
            st.caption("(no NFT receipt)")

    with tabs[5]:
        st.subheader("DIDs + Card NFTs")
        st.json(
            {
                "Traveller (root, NFT owner)": r["traveller_did"],
                "ConciergeAgent":              r["concierge_did"],
                "ConciergeAgent card NFT":     r.get("concierge_card"),
                "FlightAgent":                 r["flight_did"],
                "FlightAgent card NFT":        r.get("flight_card"),
                "BookingAgent (resource)":     r["booking_did"],
            },
            expanded=True,
        )


for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            st.markdown(entry["content"])
    elif entry["role"] == "result":
        with st.chat_message("assistant"):
            _render_result(entry["content"])

if plan_clicked and intent.strip():
    st.session_state.history.append({"role": "user", "content": intent})

    with st.chat_message("user"):
        st.markdown(intent)

    with st.chat_message("assistant"):
        with st.spinner("Running the chain..."):
            try:
                result = planner.plan(
                    intent.strip(),
                    rogue_concierge=rogue_concierge,
                    wrong_delegation=wrong_delegation,
                    constraint_breach=constraint_breach,
                )
            except Exception as e:
                st.error(f"Error: {e}")
                st.stop()

        _render_result(result)

    st.session_state.history.append({"role": "result", "content": result})
    st.rerun()
