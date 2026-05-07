import streamlit as st
from pathlib import Path
from dotenv import load_dotenv
from anthropic import Anthropic
from pipeline import DocumentAnalysisPipeline

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

# ── singleton setup ──────────────────────────────────────────────────────────

def _init_pipeline() -> DocumentAnalysisPipeline:
    client = Anthropic()
    pipeline = DocumentAnalysisPipeline(client)
    pipeline.setup()
    return pipeline

if "pipeline" not in st.session_state:
    st.session_state.pipeline = _init_pipeline()

pipeline: DocumentAnalysisPipeline = st.session_state.pipeline

# ── page config ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Document Analysis Pipeline", layout="wide")
st.title("Document Analysis Pipeline")
st.caption("Coordinator → Summarizer + Fact Checker → Synthesis  ·  Powered by Anthropic Claude")

# ── session state ────────────────────────────────────────────────────────────

if "history" not in st.session_state:
    st.session_state.history = []

# ── sidebar ──────────────────────────────────────────────────────────────────

EXAMPLE_DOCS = {
    "Climate Policy Brief": (
        "Climate change represents one of the most pressing challenges of the 21st century. "
        "According to the IPCC, global temperatures have risen 1.1°C above pre-industrial levels, "
        "with 97% of climate scientists agreeing on human causation. The Paris Agreement, signed by "
        "196 parties, aims to limit warming to 1.5°C. However, current national pledges put us on "
        "track for 2.7°C of warming by 2100. Renewable energy adoption has accelerated, with solar "
        "costs dropping 89% since 2010. Electric vehicle sales reached 14 million units in 2023, "
        "representing 18% of all car sales. Despite this progress, global CO2 emissions reached a "
        "record 37.4 billion tonnes in 2023. Immediate, transformative action is required across all "
        "sectors of the economy to avoid the worst outcomes."
    ),
    "Startup Pitch": (
        "TechVenture AI is revolutionizing the $500 billion enterprise software market with our "
        "proprietary neural architecture delivering 10x performance over existing solutions. Launched "
        "6 months ago, we already serve 500 enterprise clients with $2M ARR and 300% month-over-month "
        "growth. Our founding team brings 50 years of combined experience at Google, Meta, and OpenAI, "
        "with 12 breakthrough algorithms patent-pending. Gartner predicts our TAM will reach $2 trillion "
        "by 2030. We seek $50M Series A to grow from 15 to 150 engineers and capture 10% market share "
        "in 18 months."
    ),
    "Medical Abstract": (
        "Background: Cardiovascular disease accounts for 17.9 million deaths annually. "
        "Objective: This randomized controlled trial evaluated XR-2241 in reducing major adverse "
        "cardiovascular events (MACE). Methods: 12,450 patients with established coronary artery disease "
        "were randomized to XR-2241 (n=6,225) or placebo (n=6,225) over 48 months across 340 clinical "
        "sites. Results: XR-2241 reduced MACE by 23% (HR 0.77, 95% CI 0.69-0.86, p<0.001). All-cause "
        "mortality decreased 15%. Adverse event discontinuation occurred in 8.2% vs 6.1% of patients. "
        "Conclusion: XR-2241 demonstrates significant cardiovascular benefit with an acceptable safety "
        "profile, warranting consideration for guideline inclusion."
    ),
}

st.sidebar.header("Quick Examples")
for title in EXAMPLE_DOCS:
    if st.sidebar.button(title, use_container_width=True, key=f"ex_{title}"):
        st.session_state["doc_textarea"] = EXAMPLE_DOCS[title]
        st.rerun()

st.sidebar.divider()
st.sidebar.subheader("Agent Trace")

if pipeline.last_trace_url:
    st.sidebar.success(f"[View trace in Langfuse]({pipeline.last_trace_url})")
else:
    st.sidebar.caption("Run an analysis to see the trace link here.")

st.sidebar.divider()
if st.sidebar.button("Clear History"):
    st.session_state.history = []
    st.rerun()

# ── document input ────────────────────────────────────────────────────────────

doc_input = st.text_area(
    "Paste a document to analyze",
    key="doc_textarea",
    height=220,
    placeholder="Paste any document — article, report, research paper, pitch deck...",
)

uploaded = st.file_uploader("Or upload a .txt or .md file", type=["txt", "md"])
if uploaded:
    st.session_state["doc_textarea"] = uploaded.read().decode("utf-8", errors="ignore")
    st.rerun()

analyze = st.button("Analyze", type="primary", disabled=not doc_input.strip())

# ── history ───────────────────────────────────────────────────────────────────

for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            preview = entry["content"][:300] + "..." if len(entry["content"]) > 300 else entry["content"]
            st.markdown(f"**Document:** {preview}")
    elif entry["role"] == "result":
        with st.chat_message("assistant"):
            r = entry["content"]
            tab1, tab2, tab3 = st.tabs(["Final Report", "Summary", "Fact Check"])
            with tab1:
                st.markdown(r["synthesis"])
            with tab2:
                st.markdown(r["summary"])
            with tab3:
                st.markdown(r["fact_check"])
            if entry.get("trace_url"):
                st.caption(f"[View agent trace in Langfuse]({entry['trace_url']})")

# ── run analysis ──────────────────────────────────────────────────────────────

if analyze and doc_input.strip():
    st.session_state.history.append({"role": "user", "content": doc_input})

    with st.chat_message("user"):
        preview = doc_input[:300] + "..." if len(doc_input) > 300 else doc_input
        st.markdown(f"**Document:** {preview}")

    with st.chat_message("assistant"):
        with st.spinner("Summarizer → Fact Checker → Coordinator..."):
            try:
                result = pipeline.analyze(doc_input.strip())
            except Exception as e:
                st.error(f"Error: {e}")
                st.stop()

        tab1, tab2, tab3 = st.tabs(["Final Report", "Summary", "Fact Check"])
        with tab1:
            st.markdown(result["synthesis"])
        with tab2:
            st.markdown(result["summary"])
        with tab3:
            st.markdown(result["fact_check"])

        if pipeline.last_trace_url:
            st.caption(f"[View agent trace in Langfuse]({pipeline.last_trace_url})")

    st.session_state.history.append({
        "role": "result",
        "content": result,
        "trace_url": pipeline.last_trace_url,
    })

    st.rerun()
