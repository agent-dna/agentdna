import os
import streamlit as st
from pathlib import Path
from dotenv import load_dotenv
from google import genai

from agentdna import AgentDNA
from pipeline import ResearchPipeline

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY") or None
DEFAULT_USER_ALIAS = "Research_Head_Coordinator_USER"

# ── page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Research Assistant", layout="wide")
st.title("Research Assistant")
st.caption("Coordinator → 3 Specialist Researchers → Synthesizer  ·  Powered by Google Gemini + AgentDNA")

# ── session state ─────────────────────────────────────────────────────────────

if "history" not in st.session_state:
    st.session_state.history = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []
if "user_alias" not in st.session_state:
    st.session_state.user_alias = DEFAULT_USER_ALIAS

# ── user identity (top of the trust chain) ────────────────────────────────────
# Users own the audit-log NFT. Per signed-in alias we create one AgentDNA with
# enable_nft=True (default) — its DID is the chain-side identity that holds
# every audit record. The host & remotes are pure signers (enable_nft=False).

st.sidebar.subheader("Signed in as")
new_alias = st.sidebar.text_input(
    "User alias",
    value=st.session_state.user_alias,
    help="Your chain identity. Each unique alias gets its own DID + audit-log NFT.",
)

# Re-init pipeline if user changes alias
if new_alias != st.session_state.user_alias or "pipeline" not in st.session_state:
    st.session_state.user_alias = new_alias
    user_dna = (
        AgentDNA(alias=new_alias, api_key=AGENTDNA_API_KEY)
        if AGENTDNA_API_KEY else None
    )
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    st.session_state.pipeline = ResearchPipeline(
        client,
        agentdna_api_key=AGENTDNA_API_KEY,
        user_dna=user_dna,
    )

pipeline: ResearchPipeline = st.session_state.pipeline

st.sidebar.divider()

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
        st.session_state.chain_history = pipeline.history()
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
        # st.json renders a foldable tree that wraps long values (DIDs, sigs,
        # response_sha256, the 600-char signed response) instead of forcing
        # horizontal scroll like st.code(language="json") did.
        st.json(st.session_state.chain_history, expanded=2)

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
