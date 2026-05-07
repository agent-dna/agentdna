import json
import os
import streamlit as st
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from pipeline import ResearchPipeline

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# ── singleton setup ───────────────────────────────────────────────────────────

def _init_pipeline() -> ResearchPipeline:
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    agentdna_api_key = os.environ.get("AGENTDNA_API_KEY") or None
    return ResearchPipeline(client, agentdna_api_key=agentdna_api_key)

if "pipeline" not in st.session_state:
    st.session_state.pipeline = _init_pipeline()

pipeline: ResearchPipeline = st.session_state.pipeline

# ── chain history helper ──────────────────────────────────────────────────────

def _fetch_chain_history(nft_token: str) -> list:
    try:
        from agentdna import NodeClient
        from rubix.client import RubixClient
        from rubix.querier import Querier
        node = NodeClient()
        client = RubixClient(node_url=node.get_base_url(), timeout=300)
        q = Querier(client)
        states = q.get_nft_states(nft_address=nft_token, only_latest_state=False)
        if isinstance(states, list):
            return states
        if isinstance(states, dict):
            return [states]
        return []
    except Exception as exc:
        return [{"error": str(exc)}]


def _decode_nft_state(state: dict) -> dict:
    state = dict(state)
    nft_data = state.get("NFTData")
    if isinstance(nft_data, str):
        try:
            state["NFTData"] = json.loads(nft_data)
        except Exception:
            pass
    return state

# ── page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Research Assistant", layout="wide")
st.title("Research Assistant")
st.caption("Coordinator → 3 Specialist Researchers → Synthesizer  ·  Powered by Google Gemini + AgentDNA")

# ── session state ─────────────────────────────────────────────────────────────

if "history" not in st.session_state:
    st.session_state.history = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []

# ── sidebar ───────────────────────────────────────────────────────────────────

EXAMPLE_QUESTIONS = [
    "Why are bee populations declining and what are the consequences?",
    "How does quantum computing threaten current encryption standards?",
    "What is the impact of social media on teenage mental health?",
    "What causes income inequality and how can it be addressed?",
]

st.sidebar.header("Quick Examples")
for q in EXAMPLE_QUESTIONS:
    if st.sidebar.button(q, use_container_width=True, key=f"ex_{q}"):
        st.session_state["question_input"] = q
        st.rerun()

st.sidebar.divider()

# ── Langfuse trace link ───────────────────────────────────────────────────────
st.sidebar.subheader("Agent Trace")
if pipeline.last_trace_url:
    st.sidebar.success(f"[View trace in Langfuse]({pipeline.last_trace_url})")
    st.sidebar.caption("coordinator → researchers → synthesizer → host.verify")
else:
    st.sidebar.caption("Run a query to see the Langfuse trace here.")

st.sidebar.divider()

# ── NFT chain history controls ────────────────────────────────────────────────
st.sidebar.subheader("Chain History")

nft_token = pipeline.last_nft_token or ""
st.sidebar.caption(f"NFT: `{nft_token}`" if nft_token else "NFT: (none yet — run a query)")

if st.sidebar.button("History Records", use_container_width=True, disabled=not nft_token):
    with st.spinner("Fetching NFT data…"):
        states = _fetch_chain_history(nft_token)
    if isinstance(states, list):
        st.session_state.chain_history = [_decode_nft_state(s) for s in states]
    elif isinstance(states, dict):
        st.session_state.chain_history = [_decode_nft_state(states)]
    else:
        st.session_state.chain_history = []
    st.rerun()

st.sidebar.divider()


if st.sidebar.button("Clear History", use_container_width=True):
    st.session_state.history = []
    st.session_state.chain_history = []
    st.rerun()

# ── chain history panel ───────────────────────────────────────────────────────

if st.session_state.chain_history:
    with st.expander(
        f"Chain History — NFT `{nft_token}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.code(
            json.dumps(st.session_state.chain_history, indent=2, ensure_ascii=False),
            language="json",
        )

# ── input ─────────────────────────────────────────────────────────────────────

question = st.text_input(
    "Enter a research question",
    key="question_input",
    placeholder="e.g. What causes income inequality and how can it be addressed?",
)

research = st.button("Research", type="primary", disabled=not question.strip())

# ── render helper ─────────────────────────────────────────────────────────────

def _render_result(r: dict, trace_url: str | None = None):
    tab_labels = ["Final Report"] + [
        f"Researcher {i + 1}" for i in range(len(r["subtopics"]))
    ]
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        st.markdown(r["synthesis"])

    for i, (subtopic, finding) in enumerate(zip(r["subtopics"], r["findings"])):
        with tabs[i + 1]:
            st.caption(f"Subtopic: **{subtopic}**")
            st.markdown(finding)

    if trace_url:
        st.caption(f"[View full agent trace in Langfuse]({trace_url})")

# ── history ───────────────────────────────────────────────────────────────────

for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            st.markdown(entry["content"])
    elif entry["role"] == "result":
        with st.chat_message("assistant"):
            _render_result(entry["content"], entry.get("trace_url"))

# ── run research ──────────────────────────────────────────────────────────────

if research and question.strip():
    st.session_state.history.append({"role": "user", "content": question})

    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner(
            "Coordinator signing tasks → Researchers verifying & working → "
            "Synthesizer → Coordinator verifying all + writing to chain…"
        ):
            try:
                result = pipeline.research(question.strip())
            except Exception as e:
                st.error(f"Error: {e}")
                st.stop()

        _render_result(result, pipeline.last_trace_url)

    st.session_state.history.append({
        "role": "result",
        "content": result,
        "trace_url": pipeline.last_trace_url,
    })

    st.rerun()
