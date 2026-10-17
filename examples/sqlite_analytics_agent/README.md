# SQLite Analytics Agent

An independent CrewAI workload that analyses a local SQLite database through a FastMCP server. The database and deterministic sample data initialise automatically, and the agent can only inspect it through MCP.

## Architecture

`sqlite-analytics-agent` uses CrewAI to plan an analytical task. It discovers the MCP tools with CrewAI's own MCP client (`MCPServerAdapter` from `crewai-tools`), connected over Streamable HTTP to `mcp_server.py`.

```text
sqlite-analytics-agent -> MCPServerAdapter -> AgentDNA Provenance Relay -> FastMCP -> SQLite
```

AgentDNA signs the agent's MCP tool calls through the Provenance Relay, which runs inside the agent's process. The CrewAI MCP client carries no AgentDNA patch; it is pointed at the relay session's URL instead of the MCP server's:

```py
PROVENANCE_RELAY = ProvenanceRelay({"sqlite": settings.sqlite_mcp_url})

with PROVENANCE_RELAY.session(SQLITE_AGENT, adna_workflow) as session:   # verifies the incoming workflow
    with sqlite_tools(session.url("sqlite")) as tools:              # MCPServerAdapter on the session URL
        output = Crew(...).kickoff()

reply_workflow = SQLITE_AGENT.build(payload=str(output), previous_workflows=session.workflows)
```

When a tool result cannot be verified, the relay returns an error in its place, so the agent never acts on output that is missing from the provenance chain.

When the MCP server refuses a tool call (its whitelist, CoCA or CBAC check fails), it records the refusal and the chain ends. The relay refuses the agent's further tool calls, and the run stops with an error such as `Agent execution failed: ... the run's chain has ended: Agent <id> not whitelisted`. Nothing is recorded on the agent's side, since the server already recorded the refusal.

## Setup

1. Use Python 3.11 or 3.12 and create a virtual environment:

```
python3 -m venv .venv
source ./.venv/bin/activate
```

2. Install: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`.
4. Set `LLM_BACKEND` to `ollama`, `openai`, or `gemini`, then configure that provider's model and credentials.

Ollama uses `OLLAMA_HOST` and `OLLAMA_MODEL`. OpenAI uses `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL`; use the OpenAI-compatible `/v1` endpoint. A root gateway URL such as `https://llm.agentdna.io` is normalized to `https://llm.agentdna.io/v1`. Gemini uses `GOOGLE_API_KEY` and `GEMINI_MODEL`.

The default `SQLITE_DATABASE_PATH` is `data/analytics.db`. On its first tool call, the MCP server creates the schema and sample dataset. No manual seeding is needed for normal runs.

Set the following AgentDNA env vars:

```
AGENTDNA_API_KEY     : AgentDNA API key
PROVENANCE_LAYER_URL : Provenance Layer URL (if unset, it defaults to https://chain-connector-2.rubix.net)
ADMIN_SERVER_URL     : AgentDNA admin server, used by the MCP server for the agent whitelist check
                       (if unset, it defaults to https://agentdna-admin.agentdna.io)

USER_NAME            : Name of the user
SQLITE_AGENT_NAME    : Name of the agent
MCP_SERVER_NAME      : Name of the MCP server's AgentDNA identity (defaults to sqlite-analytics-mcp)

CBAC_URL             : CBAC decision service; leave empty for the default (production) service
```

The MCP server listens on `MCP_HOST`:`MCP_PORT` (default `127.0.0.1:8544`), and the agent reaches it at `SQLITE_MCP_URL` (default `http://127.0.0.1:8544/mcp`). `SQLITE_MCP_HEADERS`, `AGENTDNA_FINGERPRINT_KEY` and `AGENTDNA_AUTH_EVIDENCE_FILE` are optional: they are only for AgentDNA's authentication evidence.

5. Whitelist the agent on the AgentDNA admin server: the MCP server refuses tool calls from agents that aren't whitelisted. The agent's ID is printed when it is created (`agentdna.init.success`), on the first run.

## Seed Data

For local development or to prepare the database before an agent run, use the idempotent seed CLI from this project directory:

```sh
python3 seed_data.py
```


## Run Modes

### Manual

From this project directory, start the MCP server, then pass an analysis task to the Python CLI:

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 manual.py "Explore the analytics database's tables and their schema, then identify high-revenue products and unusual pending-order trends."
```
 The manual CLI prints a human-readable report by default. Add `--json` after the prompt when a script needs the structured execution event instead. On Windows, replace `python3` with `python`.

#### Example prompts

```sh
# Top products by revenue, and orders stuck in pending
python3 manual.py "Explore the analytics database's tables and their schema, then identify high-revenue products and unusual pending-order trends."

# Customers ranked by spend
python3 manual.py "Explore the analytics database's tables and their schema, then compare customers by total order value and flag any with unusually high spending."

# Revenue by product category
python3 manual.py "Explore the analytics database's tables and their schema, then summarise revenue by product category and recommend which categories need a closer look."
```

Each prompt names exploring the tables and schema because the agent always starts there, and CBAC judges every tool call against the user's request: a request that doesn't mention it can have the first call, `sqlite_list_tables`, refused as drifting from the user's intent.

### Automated

`automated.py` randomly selects one of ten predefined read-only analytical tasks for every execution. With the MCP server running (`python3 mcp_server.py`):

```
python3 automated.py
```

## MCP Tools And Security

The FastMCP server exposes `sqlite_list_tables`, `sqlite_describe_table`, and `sqlite_query`, behind AgentDNA's middleware: every tool call must carry a signed IntentWorkflow from a whitelisted agent, and the CBAC service (the `cbac` package's `authorize`, pointed at `CBAC_URL` when it is set) decides whether the call fits the user's intent. The server validates table names, accepts one `SELECT` statement only, opens the database read-only for query execution, and rejects DDL, writes, pragmas, attachments, and multiple statements. The agent must discover the schema; it is not embedded in the prompt.

The named agent contract is in [agents/sqlite_agent/SKILLS.md](agents/sqlite_agent/SKILLS.md). JSON output includes the stable agent identity and per-run execution ID.

## Tests

Run `python -m pytest tests -q` from this directory. The tests verify automatic database initialisation and destructive-query rejection.
