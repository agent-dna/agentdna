# LangGraph Fan-out Research

An orchestrator agent splits a research question into subtasks at runtime and spawns one worker agent per subtask; the workers run in parallel. Gemini is the LLM, and the workers' tools come from a FastMCP server that uses public data needing no API keys.

Each spawned worker is an AgentDNA **sub-agent** of the orchestrator: an identity of its own, created at runtime, that inherits the orchestrator's policy and whitelisting. The whole run ends as one signed chain of custody.

## Architecture

```text
                      orchestrator: plan               LangGraph + Gemini
                 ┌───────────┼───────────┐
             Send│       Send│       Send│              spawned at runtime, up to MAX_WORKERS
                 ▼           ▼           ▼
             worker-1    worker-2    worker-3          ReAct agents, in parallel; each a sub-agent
                 │           │           │
                 └── AgentDNA Provenance Relay ──┘     one session per worker
                             │
                 research MCP server (FastMCP 3.4.7 + AgentDNA middleware + CBAC)
                             │
                   World Bank · Wikipedia
                 ┌───────────┼───────────┐
                 ▼           ▼           ▼
                      orchestrator: synthesize
```

- **`plan`** asks Gemini for independent subtasks (structured output). `fan_out` returns one LangGraph `Send("worker", ...)` per subtask, so the number of workers is decided by the question rather than by the code.
- **`worker`** is a `create_react_agent` over the MCP tools.
- **`synthesize`** runs once every worker has reported, and combines the reports into the answer.

## How AgentDNA fits in

- **The user** (`runner.py`) signs the question, and records the finished chain on the provenance layer.
- **The orchestrator** (`identities.py`) is a registered agent, with its policy in `policies/orchestrator.md`. In `plan`, it verifies the user's request, then for each subtask:
  1. spawns a worker with `spawn_worker()`, which calls `ORCHESTRATOR.create_sub_agent()`. The worker is named `<orchestrator's name>-<7 random characters>`, and is ready to sign as itself;
  2. signs a delegation of the subtask, addressed to that worker.
- **Each worker** opens a Provenance Relay session as itself, on its delegation (`mcp_client.py` has the relay), and points its MCP client at the session's URL, so every tool call is signed as that worker. It then signs its report on what the session produced, addressed to the orchestrator.
- **In `synthesize`**, the orchestrator verifies every report, and signs the final answer on all of them.
- **The MCP server** (`mcp_server.py`) runs `AgentDNAMCPMiddleware`: every tool call must come from a whitelisted agent, pass CoCA verification, and be allowed by CBAC.

```py
worker = await asyncio.to_thread(spawn_worker)   # a new sub-agent of the orchestrator
delegation = ORCHESTRATOR.build(payload=..., previous_workflows=incoming, recipient_id=worker.get_actor_id())

async with PROVENANCE_RELAY.session(worker, delegation) as session:
    tools = await load_tools(session.url("research"))
    ...
report = worker.build(payload=..., previous_workflows=session.workflows, recipient_id=ORCHESTRATOR.get_actor_id())
```

The chain records which worker did what: each worker's tool calls and report descend from its own delegation, signed with its own actor ID.

- **A failed worker still reports.** If a worker raises (a Gemini rate limit, say), it signs a failure report on its delegation and the others carry on; the synthesis says what the failed worker leaves unanswered.
- **A refused tool call stops the run.** If the MCP server refuses a tool call (whitelist, CoCA or CBAC), it records the refusal and the whole run stops with an error saying the run's chain has ended. Nothing is recorded on the agents' side.

## Prerequisites

- Python 3.10–3.12
- A Gemini API key, from Google AI Studio
- An AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io)

## Setup

1. Create and activate a virtual environment, from this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

2. Install the dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, and set the variables below.
4. Run it once (see [Run](#run)). The orchestrator's AgentDNA ID is printed when it starts (`agentdna.init.success`). Whitelist that ID on the AgentDNA admin server: until then, the MCP server refuses the workers' tool calls. The workers are the orchestrator's sub-agents, so they're whitelisted through it and need no approval of their own.

### Environment variables

**Gemini**

| Variable | Description | Default |
|---|---|---|
| `GOOGLE_API_KEY` | Gemini API key, from Google AI Studio. Required. | |
| `GEMINI_MODEL` | Gemini model the orchestrator and every worker use. | `gemini-2.5-flash` |
| `LLM_TEMPERATURE` | Sampling temperature. | `0` |

**Fan-out**

| Variable | Description | Default |
|---|---|---|
| `MAX_WORKERS` | The most workers the orchestrator may spawn for one question. | `4` |

Each worker makes several Gemini calls, and the workers run at the same time. On a free-tier Gemini key, a lower `MAX_WORKERS` avoids rate limits.

**Research MCP server**

| Variable | Description | Default |
|---|---|---|
| `RESEARCH_MCP_URL` | The URL the workers reach the MCP server at. It must match `MCP_HOST` and `MCP_PORT`. | `http://127.0.0.1:8670/mcp` |
| `MCP_HOST` | Interface the MCP server listens on. | `127.0.0.1` |
| `MCP_PORT` | Port the MCP server listens on. | `8670` |
| `HTTP_TIMEOUT_SECONDS` | Timeout for the tools' requests to the World Bank and Wikipedia, and for the workers' requests to the MCP server. | `45` |

**Wikipedia**

| Variable | Description | Default |
|---|---|---|
| `CONTACT` | A URL or email to reach you by. It goes in the User-Agent of Wikipedia requests, as [Wikimedia's robot policy](https://w.wiki/4wJS) requires; without it, Wikipedia may answer `403 Please respect our robot policy`. | |
| `WIKIPEDIA_CONCURRENCY` | The most Wikipedia requests in flight at once, across every worker. Requests back off when Wikipedia answers 429 or 503. | `2` |

**AgentDNA**

| Variable | Description | Default |
|---|---|---|
| `AGENTDNA_API_KEY` | Your AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io). Required. | |
| `PROVENANCE_LAYER_URL` | The AgentDNA Provenance Layer URL. | `https://chain-connector-2.rubix.net` |
| `ADMIN_SERVER_URL` | The AgentDNA admin server, which the MCP server asks whether an agent is whitelisted. | `https://agentdna-admin.agentdna.io` |
| `USER_NAME` | Name of the user identity that signs the question and records the result. Required. | |
| `ORCHESTRATOR_AGENT_NAME` | Name of the orchestrator's identity. Each worker is named `<this name>-<7 random characters>`. | `research-orchestrator` |
| `MCP_SERVER_NAME` | Name of the MCP server's identity. | `research-mcp` |
| `CBAC_URL` | The CBAC decision service the MCP server asks whether each tool call fits the user's intent. Leave empty for the default (production) service. | |

**Authentication evidence (optional)**

`AGENTDNA_AUTH_EVIDENCE`, `AGENTDNA_AUTH_EVIDENCE_FILE` and `AGENTDNA_FINGERPRINT_KEY` switch on AgentDNA's authentication evidence, which is off by default. See [Authentication evidence](../../README.md#authentication-evidence) in the main README.

## Run

From this directory, start the MCP server, then run the agents in a second terminal:

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 manual.py "Find Kenya, Germany and India in the World Bank data, and show their renewable energy share and internet users since 2015."
```

`manual.py` prints the workers the orchestrator spawned and whether each succeeded, then the synthesized answer. Add `--json` after the prompt to print the structured execution event instead. Either way, the run's chain is recorded on the provenance layer. On Windows, use `python` instead of `python3`.

### Example prompts

```sh
# Indicators for several countries
python3 manual.py "Find Kenya, Germany and India in the World Bank data, and show their renewable energy share and internet users since 2015."

# Indicators plus background
python3 manual.py "Find Nigeria, Bangladesh and Vietnam in the World Bank data, show their electricity access and GDP per capita since 2015, and summarise each country's Wikipedia article."

# One indicator across countries
python3 manual.py "Find Norway, Iceland and Costa Rica in the World Bank data, and show the renewable energy share of each since 2015."
```

CBAC judges every tool call against the user's question, and its check is probabilistic. A question that asks for what the tools do (find countries, show World Bank indicators, summarise Wikipedia articles) is less likely to have a call refused as drifting from the user's intent than one that asks for a broad judgement.

### Automated

`automated.py` runs continuously until you stop it (Ctrl+C): each run picks one of five built-in questions at random, prints the JSON execution event, and records the chain.

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 automated.py
```

## MCP tools

| MCP tool | Data source (no API key) |
|---|---|
| `find_country` | World Bank country metadata: ISO3 code, region, income level, capital |
| `country_indicator` | World Bank indicators: population, GDP per capita, renewable energy share, electricity access, internet users |
| `wikipedia_search`, `wikipedia_summary` | Wikipedia search and REST APIs |

The orchestrator's skills and restrictions, which its workers inherit, are in [policies/orchestrator.md](policies/orchestrator.md). It is also the policy registered with the orchestrator's AgentDNA identity.

## Tests

From this directory: `python -m pytest tests -q`. The tests need no network and no keys: they run the real graph with a scripted LLM, scripted workers, and stand-in AgentDNA identities and relay. They check:
- the tools' input validation;
- one worker per subtask, and at most `MAX_WORKERS` of them;
- that a failed worker still reports, and the final answer is synthesized from every report;
- the chain: one new sub-agent and one delegation per worker, each report signed by its worker, and the answer built on all of them;
- that a refused tool call stops the run, and a request that fails verification is recorded and refused.
