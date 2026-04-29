# Research Assistant

A multi-agent research pipeline powered by **Google Gemini** with full agent tracing in **Langfuse**.

## Architecture

```
User question
  └─► Coordinator        — breaks question into 3 focused subtopics
        ├─► Researcher 1  — investigates subtopic 1
        ├─► Researcher 2  — investigates subtopic 2
        ├─► Researcher 3  — investigates subtopic 3
        └─► Synthesizer   — combines all findings into a final report
```

Every agent call is traced in Langfuse. After each run a direct link to the trace appears in the UI and sidebar.

## Prerequisites

1. **Python 3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/)
2. A **Google API key** — [get one free at aistudio.google.com](https://aistudio.google.com/app/apikey)
3. A **Langfuse account** — [cloud.langfuse.com](https://cloud.langfuse.com) (free tier)

## Setup

```bash
cp .env.sample .env
# Fill in GOOGLE_API_KEY and Langfuse keys
```

## Run

```bash
./run.sh
```

Opens the Streamlit UI at `http://localhost:8501`.

## Viewing the trace in Langfuse

After running a research query:

1. Click the **"View trace in Langfuse"** link in the UI or sidebar
2. You'll see the full agent coordination flow:
   - `research-assistant` — top-level trace
   - `coordinator` — subtopic planning span
   - `researcher` × 3 — one span per subtopic
   - `synthesizer` — final report span
3. Each span shows input, output, latency, and token usage
