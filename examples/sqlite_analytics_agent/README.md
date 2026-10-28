# SQLite Analytics Agent

A CrewAI agent that analyses a local SQLite database through a FastMCP server. The database and its sample data are created automatically, and the agent can only reach the database through the MCP server's read-only tools.

## Architecture

```text
sqlite-analytics-agent -> MCPServerAdapter -> AgentDNA Provenance Relay -> FastMCP + AgentDNA middleware -> SQLite
```

- `manual.py` / `automated.py`: the user signs the request, the agent runs, and the user records the finished chain on the provenance layer.
- `agents/sqlite_agent/agent.py`: the agent. It opens a Provenance Relay session on the user's workflow, and points CrewAI's MCP client (`MCPServerAdapter`, in `mcp_client.py`) at the session's URL, so every tool call is signed as the agent.
- `mcp_server.py`: the SQLite MCP server, behind `AgentDNAMCPMiddleware`. Every tool call must come from a whitelisted agent, pass CoCA verification, and be allowed by CBAC.

```py
PROVENANCE_RELAY = ProvenanceRelay({"sqlite": settings.sqlite_mcp_url})

with PROVENANCE_RELAY.session(SQLITE_AGENT, adna_workflow) as session:   # verifies the incoming workflow
    with sqlite_tools(session.url("sqlite")) as tools:                   # MCPServerAdapter on the session URL
        output = Crew(...).kickoff()

# session.workflows: the user's workflow plus every tool call
reply_workflow = SQLITE_AGENT.build(payload=str(output), previous_workflows=session.workflows)
```

If the MCP server refuses a tool call (whitelist, CoCA or CBAC), it records the refusal and the run stops with an error saying the run's chain has ended, along with the reason. Nothing is recorded on the agent's side.

## Prerequisites

- Python 3.11 or 3.12
- An LLM: Ollama, OpenAI (or an OpenAI-compatible service), or Gemini
- An AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io)

## Setup

1. Create and activate a virtual environment, from this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

2. Install the dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, and set the variables below.
4. Run the agent once (see [Run](#run)). Its AgentDNA ID is printed when it starts (`agentdna.init.success`). Whitelist that ID on the AgentDNA admin server: until then, the MCP server refuses its tool calls.

The database (`SQLITE_DATABASE_PATH`) is created with its schema and sample data on the MCP server's first tool call. To create it beforehand, run `python3 seed_data.py` from this directory; it is safe to run more than once.

### Environment variables

**LLM**

| Variable | Description | Default |
|---|---|---|
| `LLM_BACKEND` | `ollama`, `openai` or `gemini`. | `ollama` |
| `LLM_TEMPERATURE` | Sampling temperature. | `0` |
| `OLLAMA_HOST` | Ollama server URL. | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Ollama model; pull it first (`ollama pull <model>`). | `llama3.1` |
| `OPENAI_API_KEY` | OpenAI API key. Required for `openai`. | |
| `OPENAI_MODEL` | OpenAI model. | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | Base URL of the OpenAI(-compatible) API. Required for `openai`; `/v1` is added when missing, so `https://llm.agentdna.io` becomes `https://llm.agentdna.io/v1`. | |
| `GOOGLE_API_KEY` | Gemini API key, from Google AI Studio. Required for `gemini`. | |
| `GEMINI_MODEL` | Gemini model. | `gemini-3.5-flash-lite` |

**SQLite and MCP**

| Variable | Description | Default |
|---|---|---|
| `SQLITE_DATABASE_PATH` | The SQLite database file. It is created when missing. | `data/analytics.db` |
| `SQLITE_MCP_URL` | The URL the agent reaches the MCP server at. It must match `MCP_HOST` and `MCP_PORT`. | `http://127.0.0.1:8544/mcp` |
| `MCP_HOST` | Interface the MCP server listens on. | `127.0.0.1` |
| `MCP_PORT` | Port the MCP server listens on. | `8544` |

**AgentDNA**

| Variable | Description | Default |
|---|---|---|
| `AGENTDNA_API_KEY` | Your AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io). Required. | |
| `PROVENANCE_LAYER_URL` | The AgentDNA Provenance Layer URL. | `https://chain-connector-2.rubix.net` |
| `ADMIN_SERVER_URL` | The AgentDNA admin server, which the MCP server asks whether every actor in the incoming workflow is whitelisted. | `https://agentdna-admin.agentdna.io` |
| `USER_NAME` | Name of the user identity that signs requests and records results. Required. | |
| `SQLITE_AGENT_NAME` | Name of the agent's identity. Required. | |
| `MCP_SERVER_NAME` | Name of the MCP server's identity. | `sqlite-analytics-mcp` |
| `CBAC_URL` | The CBAC decision service the MCP server asks whether each tool call fits the user's intent. Leave empty for the default (production) service. | |

**Authentication evidence (optional)**

| Variable | Description | Default |
|---|---|---|
| `SQLITE_MCP_HEADERS` | HTTP headers the agent sends to the MCP server, as a JSON object, for example `{"Authorization": "Bearer <token>"}`. They give a run a credential for authentication evidence to observe; the SQLite tools don't need them. `python3 make_token.py <user>` prints a test token. | |
| `AGENTDNA_AUTH_EVIDENCE`, `AGENTDNA_AUTH_EVIDENCE_FILE`, `AGENTDNA_FINGERPRINT_KEY` | Switch on AgentDNA's authentication evidence, which is off by default. See [Authentication evidence](../../README.md#authentication-evidence) in the main README. | |

## Run

From this directory, start the MCP server, then run the agent in a second terminal:

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 manual.py "Explore the analytics database's tables and their schema, then identify high-revenue products and unusual pending-order trends."
```

`manual.py` prints a readable report. Add `--json` after the prompt to print the structured execution event instead. On Windows, use `python` instead of `python3`.

### Example prompts

```sh
# Top products by revenue, and orders stuck in pending
python3 manual.py "Explore the analytics database's tables and their schema, then identify high-revenue products and unusual pending-order trends."

# Customers ranked by spend
python3 manual.py "Explore the analytics database's tables and their schema, then compare customers by total order value and flag any with unusually high spending."

# Revenue by product category
python3 manual.py "Explore the analytics database's tables and their schema, then summarise revenue by product category and recommend which categories need a closer look."
```

Each prompt names exploring the tables and schema because the agent always starts there, and CBAC judges every tool call against the user's prompt: a prompt that doesn't mention it can have the first call, `sqlite_list_tables`, refused as drifting from the user's intent.

### Automated

`automated.py` runs continuously until you stop it (Ctrl+C): each run picks one of ten built-in prompts at random, prints the JSON execution event, and records the chain.

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 automated.py
```

`scripts/run.sh` (and `scripts/run.ps1` / `scripts/run.cmd` on Windows) start the MCP server and run `automated.py`; stopping it stops the server too.

## MCP tools

`sqlite_list_tables`, `sqlite_describe_table` and `sqlite_query`. The server validates table names, accepts a single `SELECT` statement only, opens the database read-only for queries, and rejects DDL, writes, pragmas, attachments and multiple statements. The schema isn't in the prompt: the agent discovers it through the tools.

The agent's skills and restrictions are in [agents/sqlite_agent/SKILLS.md](agents/sqlite_agent/SKILLS.md), which is also the policy registered with its AgentDNA identity.

## Tests

From this directory: `python -m pytest tests -q`. The tests check the database's automatic creation and that destructive queries are rejected.
