# Kaitlynn Agent (LangGraph)

One of three remote agents in the [A2A Friend Scheduling demo](../README.md). Represents Kaitlynn's calendar and replies to "are you free at …?" questions over the A2A protocol.

Built on **LangGraph** + Gemini.

## Running

This agent is started automatically by `../run.sh`. To run it standalone (for debugging):

```bash
cd examples/multi_agent_system/kaitlynn_agent_langgraph
uv sync
uv run --no-sync -m app.__main__
```

The agent listens on **port 10004** by default and exposes its A2A card at <http://localhost:10004/.well-known/agent.json>.

Environment variables (`GOOGLE_API_KEY`, `AGENTDNA_API_KEY`) are loaded from the shared `.env` at `examples/multi_agent_system/.env`.

## Trust layer

Pure-remote agent — `AgentDNA(alias="kaitlynn", ..., enable_nft=False)`. Never writes to chain. On each incoming request:

1. `dna.handle(raw)` → typed `RequestContext` (host_block, original_message, trust_issues).
2. LangGraph reasoning over Kaitlynn's calendar.
3. `dna.build(reply, ctx=ctx)` → wire string returned to the host.

Only the host (`host_agent_adk`) writes to chain when it verifies the reply.
