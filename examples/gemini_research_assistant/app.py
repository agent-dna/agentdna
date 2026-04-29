import os
import streamlit as st
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from pipeline import ResearchPipeline

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# ── singleton setup ──────────────────────────────────────────────────────────

def _init_pipeline() -> ResearchPipeline:
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    agentdna_api_key = os.environ.get("AGENTDNA_API_KEY") or None
    return ResearchPipeline(client, agentdna_api_key=agentdna_api_key)

if "pipeline" not in st.session_state:
    st.session_state.pipeline = _init_pipeline()

pipeline: ResearchPipeline = st.session_state.pipeline

# ── page config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Research Assistant", layout="wide")
st.title("Research Assistant")
st.caption(
    "Coordinator → 3 Verified Specialist Researchers → Verified Synthesizer · Powered by Google Gemini + AgentDNA + Rubix"
)

# ── session state ────────────────────────────────────────────────────────────

if "history" not in st.session_state:
    st.session_state.history = []

# ── sidebar ──────────────────────────────────────────────────────────────────

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
st.sidebar.subheader("Agent Trace")

if pipeline.last_trace_url:
    st.sidebar.success(f"[View trace in Langfuse]({pipeline.last_trace_url})")
    st.sidebar.caption("See coordinator, researchers, and synthesizer as separate nodes.")
else:
    st.sidebar.caption("Run a research query to see the trace link here.")

st.sidebar.divider()
if st.sidebar.button("Clear History"):
    st.session_state.history = []
    st.rerun()

# ── input ─────────────────────────────────────────────────────────────────────

question = st.text_input(
    "Enter a research question",
    key="question_input",
    placeholder="e.g. What causes income inequality and how can it be addressed?",
)

research = st.button("Research", type="primary", disabled=not question.strip())

# ── render helper ─────────────────────────────────────────────────────────────

def _render_result(r: dict, trace_url: str | None = None):
    tab_labels = ["Final Report"] + [f"Researcher {i+1}" for i in range(len(r["subtopics"]))]
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
        with st.spinner("Coordinator planning subtopics → Researchers investigating → Synthesizing..."):
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
