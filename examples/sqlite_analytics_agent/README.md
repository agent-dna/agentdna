# SQLite Analytics Agent

An independent CrewAI workload that analyses a local SQLite database through a FastMCP stdio server. The database and deterministic sample data initialise automatically, and the agent can only inspect it through MCP.

## Architecture

`sqlite-analytics-agent` uses CrewAI to plan an analytical task. It discovers MCP tools with `langchain_mcp_adapters`, which starts `mcp_server.py` via stdio. A small bridge presents those discovered tools to CrewAI while preserving the MCP capability boundary.

```text
sqlite-analytics-agent -> langchain_mcp_adapters (stdio) -> FastMCP -> SQLite
```

## Setup

1. Use Python 3.11 or later and create a virtual environment:

```
python3 -m venv .vnev
chmod +x ./.venv/bin/activate
source ./.venv/bin/activate
```

2. Install: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`.
4. Set `LLM_BACKEND` to `ollama`, `openai`, or `gemini`, then configure that provider's model and credentials.

Ollama uses `OLLAMA_HOST` and `OLLAMA_MODEL`. OpenAI uses `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL`; use the OpenAI-compatible `/v1` endpoint. A root gateway URL such as `https://llm.agentdna.io` is normalized to `https://llm.agentdna.io/v1`. Gemini uses `GOOGLE_API_KEY` and `GEMINI_MODEL`.

The default `SQLITE_DATABASE_PATH` is `data/analytics.db`. On its first tool call, the MCP server creates the schema and sample dataset. No manual seeding is needed for normal runs.

Set the following AgentDNA env vars:

```
AGENTDNA_API_KEY : AgentDNA API Key
PROVENANCE_LAYER_URL: Provenance Layer URL (if unset, it defaults to https://chain-connector-2.rubix.net)

USER_NAME :  Name of the user
SQLITE_AGENT_NAME : Name of the Agent
```

## Seed Data

For local development or to prepare the database before an agent run, use the idempotent seed CLI from the `examples` parent directory:

```sh
python3 seed_data.py
```


## Run Modes

### Manual

From the `examples` parent directory, pass an analysis task to the Python CLI:

```sh
python  manual.py "Identify high-revenue products and unusual pending-order trends."
```

The stdio MCP server is started and managed by the adapter. The manual CLI prints a human-readable report by default. Add `--json` after the prompt when a script needs the structured execution event instead. On Windows, replace `python3` with `python`.

### Automated

`automated.py` randomly selects one of ten predefined read-only analytical tasks for every execution. The platform launchers run that entrypoint and exit after one workload:

```
python3 automated.py
```

## MCP Tools And Security

The FastMCP server exposes `sqlite_list_tables`, `sqlite_describe_table`, and `sqlite_query`. The server validates table names, accepts one `SELECT` statement only, opens the database read-only for query execution, and rejects DDL, writes, pragmas, attachments, and multiple statements. The agent must discover the schema; it is not embedded in the prompt.

The named agent contract is in [agents/sqlite_analytics_agent/SKILLS.md](agents/sqlite_analytics_agent/SKILLS.md). JSON output includes the stable agent identity and per-run execution ID.

## Tests

Run `python -m pytest tests -q` from this directory. The tests verify automatic database initialisation and destructive-query rejection.
