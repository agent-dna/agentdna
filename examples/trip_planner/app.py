import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from agent import TripPlanner

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
DEFAULT_USER_ALIAS = "Traveller_USER"

# ── page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Trip Planner", layout="wide")
st.title("Trip Planner")
st.caption(
    "Traveller → ConciergeAgent → FlightAgent → BookingAgent  ·  "
    "AgentDNA recursive Chain of Custody (depth-3)"
)

if not AGENTDNA_API_KEY:
    st.error(
        "AGENTDNA_API_KEY not set. Copy `.env.sample` to `.env` and fill it in, "
        "then restart with `./run.sh` or `uv run streamlit run app.py`."
    )
    st.stop()

# ── session state ─────────────────────────────────────────────────────────────

if "history" not in st.session_state:
    st.session_state.history = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []
if "user_alias" not in st.session_state:
    st.session_state.user_alias = DEFAULT_USER_ALIAS

# ── sidebar: user identity (top of the trust chain) ───────────────────────────

st.sidebar.subheader("Signed in as")
new_alias = st.sidebar.text_input(
    "Traveller alias",
    value=st.session_state.user_alias,
    help="Your chain identity. Each unique alias gets its own DID + audit-log NFT.",
)

# (Re)build the planner if alias changed or first run.
if new_alias != st.session_state.user_alias or "planner" not in st.session_state:
    st.session_state.user_alias = new_alias
    with st.spinner(f"Provisioning DID + NFT for {new_alias}…"):
        st.session_state.planner = TripPlanner(
            user_alias=new_alias,
            api_key=AGENTDNA_API_KEY,
        )

planner: TripPlanner = st.session_state.planner

st.sidebar.divider()

# ── sidebar: quick example trips ──────────────────────────────────────────────

EXAMPLE_TRIPS = [
    "Plan a 5-day trip to Tokyo for two adults in June.",
    "Book a long weekend in Lisbon for one traveller.",
    "Plan a 10-day road-trip-style flight tour of Iceland.",
    "Arrange a business trip: SFO → London Heathrow next Monday.",
]

st.sidebar.header("Quick Examples")
for q in EXAMPLE_TRIPS:
    if st.sidebar.button(q, use_container_width=True, key=f"ex_{q}"):
        st.session_state["intent_input"] = q
        st.rerun()

st.sidebar.divider()

# ── sidebar: NFT chain history ────────────────────────────────────────────────

st.sidebar.subheader("Chain History")

nft_token = planner.nft_token or ""
st.sidebar.caption(
    f"NFT: `{nft_token}`" if nft_token else "NFT: (will be deployed on first run)"
)

if st.sidebar.button("Fetch History Records", use_container_width=True, disabled=not nft_token):
    with st.spinner("Fetching NFT records from Rubix…"):
        st.session_state.chain_history = planner.history()
    st.rerun()

if st.sidebar.button("Clear Local History", use_container_width=True):
    st.session_state.history = []
    st.session_state.chain_history = []
    st.rerun()

# ── chain history panel ───────────────────────────────────────────────────────

if st.session_state.chain_history:
    with st.expander(
        f"Chain History — NFT `{nft_token}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.json(st.session_state.chain_history, expanded=2)

# ── input ─────────────────────────────────────────────────────────────────────

intent = st.text_input(
    "Describe your trip",
    key="intent_input",
    placeholder="e.g. Plan a 5-day trip to Tokyo for two adults in June.",
)
plan_clicked = st.button("Plan trip", type="primary", disabled=not intent.strip())

# ── render helper ─────────────────────────────────────────────────────────────

def _render_result(r: dict) -> None:
    status = "✅ verified" if r["verified"] else "❌ verification failed"
    st.markdown(
        f"**Status:** {status}  ·  "
        f"**Chain depth:** {r['chain_depth']}  ·  "
        f"**Root user verified:** {r['user_verified']}"
    )
    if r["trust_issues"]:
        st.warning("Trust issues: " + "; ".join(r["trust_issues"]))

    tabs = st.tabs([
        "Booking confirmation",
        "Concierge plan",
        "Flight search",
        "Audit (NFT write)",
        "Parties (DIDs)",
    ])

    with tabs[0]:
        st.subheader("BookingAgent — final reply")
        st.json(r["booking_payload"], expanded=True)

    with tabs[1]:
        st.subheader("ConciergeAgent — derived plan")
        st.json(r["concierge_payload"], expanded=True)

    with tabs[2]:
        st.subheader("FlightAgent — search criteria")
        st.json(r["flight_payload"], expanded=True)

    with tabs[3]:
        st.subheader("On-chain audit record")
        if r["nft_result"]:
            st.json(r["nft_result"], expanded=True)
        else:
            st.caption("(no NFT receipt — chain write skipped or failed)")

    with tabs[4]:
        st.subheader("DIDs of the four parties")
        st.json(
            {
                "Traveller (root, NFT owner)": r["traveller_did"],
                "ConciergeAgent":              r["concierge_did"],
                "FlightAgent":                 r["flight_did"],
                "BookingAgent (resource)":     r["booking_did"],
            },
            expanded=True,
        )

# ── history ───────────────────────────────────────────────────────────────────

for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            st.markdown(entry["content"])
    elif entry["role"] == "result":
        with st.chat_message("assistant"):
            _render_result(entry["content"])

# ── run pipeline ──────────────────────────────────────────────────────────────

if plan_clicked and intent.strip():
    st.session_state.history.append({"role": "user", "content": intent})

    with st.chat_message("user"):
        st.markdown(intent)

    with st.chat_message("assistant"):
        with st.spinner(
            "Traveller signs intent → Concierge plans → FlightAgent searches → "
            "BookingAgent confirms → Traveller verifies + writes audit-log NFT…"
        ):
            try:
                result = planner.plan(intent.strip())
            except Exception as e:
                st.error(f"Error: {e}")
                st.stop()

        _render_result(result)

    st.session_state.history.append({"role": "result", "content": result})
    st.rerun()
