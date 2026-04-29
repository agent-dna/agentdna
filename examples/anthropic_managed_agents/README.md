# Document Analysis Pipeline

A multi-agent document analysis system built with **Anthropic Claude Managed Agents**. The full agent trace — which agent ran, token usage per thread, and the complete event history — is visible in the [Anthropic Console](https://console.anthropic.com) → **Sessions** tab.

## Architecture

```
User
 └─► Coordinator (claude-opus-4-7)
       ├─► Document Summarizer (claude-sonnet-4-6)
       │     └─ Overview, key points, topics, structure, audience
       └─► Fact Checker (claude-sonnet-4-6)
             └─ Claim inventory, verifiability assessment, reliability verdict
```

The coordinator delegates to both subagents sequentially, then synthesizes their outputs into a final report. Each agent runs in its own isolated session thread with independent context.

## Prerequisites

1. **Python 3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/)
2. An **Anthropic API key** — [get one here](https://console.anthropic.com/settings/keys)
3. **Managed Agents access** — enabled by default for all API accounts
4. **Multiagent research preview access** — [request access here](https://claude.com/form/claude-managed-agents) (required for the coordinator → subagent delegation)

## Setup

```bash
cp .env.sample .env
# Edit .env and add your ANTHROPIC_API_KEY
```

## Run

```bash
./run.sh
```

This installs dependencies and opens the Streamlit UI. On first run, three agents and an environment are created in your Anthropic account — this takes a few seconds.

## Viewing the agent trace

After running an analysis:

1. Open the [Anthropic Console](https://console.anthropic.com)
2. Go to **Sessions** in the left sidebar
3. Find the session by the ID shown in the UI
4. Click into it to see:
   - The coordinator's primary thread
   - Each subagent's thread (Summarizer, Fact Checker)
   - Token usage, cost, and duration per agent
   - The full event history per thread

## How it differs from other examples

| | This example | `multi_agent_system` / `cicd_pipeline` |
|---|---|---|
| **Protocol** | Anthropic Managed Agents API | A2A (Agent-to-Agent) |
| **Agent hosting** | Anthropic cloud (managed) | Local processes |
| **Observability** | Anthropic Console Sessions tab | AgentDNA trust verification |
| **Orchestration** | `callable_agents` in agent config | Google ADK / CrewAI / LangGraph |
| **Setup** | Single process | Multiple processes via `run.sh` |

## References

- [Claude Managed Agents overview](https://platform.claude.com/docs/en/managed-agents/overview)
- [Multiagent sessions](https://platform.claude.com/docs/en/managed-agents/multi-agent)
- [API quickstart](https://platform.claude.com/docs/en/managed-agents/quickstart)
