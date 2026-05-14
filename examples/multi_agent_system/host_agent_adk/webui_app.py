import os, sys, uuid, asyncio
import streamlit as st
import nest_asyncio
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentdna import AgentDNA
from host.agent import HostAgent

nest_asyncio.apply()
load_dotenv()
HERE = os.path.dirname(__file__)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

REMOTE_URLS = [
    "http://localhost:10002",
    "http://localhost:10003",
    "http://localhost:10004",
]

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
DEFAULT_USER_ALIAS = "host_USER"

# -------------------------
# User identity (top of the trust chain)
# -------------------------
if "user_alias" not in st.session_state:
    st.session_state.user_alias = DEFAULT_USER_ALIAS

st.sidebar.subheader("Signed in as")
new_alias = st.sidebar.text_input(
    "User alias",
    value=st.session_state.user_alias,
    help="Your chain identity. Each unique alias gets its own DID + audit-log NFT.",
)

# Rebuild user_dna + HOST whenever alias changes.
if (
    "user_dna" not in st.session_state
    or new_alias != st.session_state.user_alias
    or "HOST" not in st.session_state
):
    st.session_state.user_alias = new_alias
    st.session_state.user_dna = (
        AgentDNA(alias=new_alias, api_key=AGENTDNA_API_KEY) if AGENTDNA_API_KEY else None
    )
    loop = asyncio.get_event_loop()
    try:
        st.session_state.HOST = loop.run_until_complete(
            HostAgent.create(
                remote_agent_addresses=REMOTE_URLS,
                user_dna=st.session_state.user_dna,
            )
        )
    except Exception:
        st.session_state.HOST = HostAgent(user_dna=st.session_state.user_dna)

st.sidebar.divider()
HOST = st.session_state.HOST

# -------------------------
# Streamlit UI
# -------------------------
st.set_page_config(page_title="A2A Host Console", layout="wide")
st.title("Pickleball Court Agent")

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "chat" not in st.session_state:
    st.session_state.chat = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []


async def consume_stream(q: str, sid: str) -> str:
    final = ""
    async for event in HOST.stream(q, sid):
        if event.get("is_task_complete"):
            final = event.get("content") or event.get("text") or ""
    return final


def run_stream(q: str) -> str:
    return asyncio.get_event_loop().run_until_complete(
        consume_stream(q, st.session_state.session_id)
    )

# 1) Render chat history
for role, msg in st.session_state.chat:
    st.chat_message(role).write(msg)

# 1b) Chain-history panel — foldable JSON tree, no horizontal overflow.
if st.session_state.chain_history:
    nft_id = (HOST.user_dna.nft_token if HOST.user_dna else "") or ""
    with st.expander(
        f"Chain History — NFT `{nft_id}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.json(st.session_state.chain_history, expanded=2)

# 2) Chat input
prompt = st.chat_input("Type a message for the Host Agent…", key="host_chat_input")

# 3) Handle new input
if prompt:
    st.session_state.chat.append(("user", prompt))

    with st.spinner("Processing Request…"):
        reply = run_stream(prompt)

    st.session_state.chat.append(("assistant", reply))
    st.rerun()

# -------------------------
# Sidebar controls
# -------------------------
st.sidebar.subheader("Controls")


def quick(q: str):
    st.session_state.chat.append(("user", f"(Quick) {q}"))
    with st.spinner("Running…"):
        reply = run_stream(q)
    st.session_state.chat.append(("assistant", reply))
    st.rerun()


st.sidebar.button("Court Availabilities", on_click=lambda: quick("/other next 3 slots"))

st.sidebar.button(
    "New Session",
    on_click=lambda: st.session_state.update(session_id=str(uuid.uuid4()), chat=[]),
)

st.sidebar.divider()

# -------------------------
# Audit-log NFT + chain history
# -------------------------
st.sidebar.subheader("Audit Log")

nft_id = (HOST.user_dna.nft_token if HOST.user_dna else "") or ""
st.sidebar.caption(f"NFT: `{nft_id}`" if nft_id else "NFT: (none yet)")

if st.sidebar.button("History Records", disabled=not nft_id):
    with st.spinner("Fetching NFT data…"):
        st.session_state.chain_history = HOST.user_dna.history()
    st.rerun()
