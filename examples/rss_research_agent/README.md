# RSS Research Agent

A LangGraph orchestrator with two worker agents, `rss-security-agent` and `rss-technology-agent`, that research public RSS and Atom feeds through a FastMCP server. The workers can only read feeds through the MCP server's tools, and only the feeds you configure.

## Architecture

```text
orchestrator -> security worker -----\
                                      -> AgentDNA Provenance Relay -> RSS MCP + AgentDNA middleware -> configured feeds
orchestrator -> technology worker ---/
```

- `manual.py` / `automated.py`: the user signs the request, the orchestrator runs, and the user records the finished chain on the provenance layer.
- `agents/rss_orchestrator_agent/agent.py`: the orchestrator. It verifies the user's workflow, delegates the task to both workers at once, verifies their signed reports, and signs a synthesis built on them.
- `agents/rss_security_agent/agent.py`, `agents/rss_technology_agent/agent.py`: the workers. Each opens its own Provenance Relay session on the orchestrator's delegation and points `langchain_mcp_adapters` at the session's URL (`mcp_client.py`), so each worker's tool calls are signed as that worker.
- `mcp_server.py`: the RSS MCP server, behind `AgentDNAMCPMiddleware`. Every tool call must come from a whitelisted agent, pass CoCA verification, and be allowed by CBAC. It keeps a local SQLite cache of articles.

```py
async with PROVENANCE_RELAY.session(RSS_SECURITY_AGENT, adna_workflow) as session:   # verifies the incoming workflow
    tools = await load_tools(session.url("rss"))
    result = await agent.ainvoke(...)

# session.workflows: the delegation plus every tool call
security_adna_workflow = RSS_SECURITY_AGENT.build(payload=..., previous_workflows=session.workflows)
```

If the MCP server refuses a worker's tool call (whitelist, CoCA or CBAC), it records the refusal and the run stops with an error saying the run's chain has ended, along with the reason. Nothing is recorded on the agents' side.

## Prerequisites

- Python 3.11 or 3.12
- An LLM: Ollama, OpenAI (or an OpenAI-compatible service), or Gemini
- An AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io)

No RSS account or API key is needed: the server reads public feeds from the URLs you configure.

## Setup

1. Create and activate a virtual environment, from this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

2. Install the dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, and set the variables below.
4. Run the agents once (see [Run](#run)). Each agent's AgentDNA ID is printed when it starts (`agentdna.init.success`). Whitelist the two workers' IDs (`RSS_SECURITY_AGENT_NAME` and `RSS_TECHNOLOGY_AGENT_NAME`) on the AgentDNA admin server: until then, the MCP server refuses their tool calls. The orchestrator calls no tools, so it doesn't need to be whitelisted.

### Environment variables

**LLM**

| Variable | Description | Default |
|---|---|---|
| `LLM_BACKEND` | `ollama`, `openai` or `gemini`. | `ollama` |
| `LLM_TEMPERATURE` | Sampling temperature. | `0` |
| `LLM_REQUEST_TIMEOUT_SECONDS` | Timeout for each request to the LLM. Applies to `openai` and `gemini`. | `120` |
| `LLM_MAX_RETRIES` | Retries for failed LLM requests. Applies to `openai` and `gemini`. | `4` |
| `OLLAMA_HOST` | Ollama server URL. | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Ollama model; pull it first (`ollama pull <model>`). | `llama3.1` |
| `OPENAI_API_KEY` | OpenAI API key. | |
| `OPENAI_MODEL_NAME` | OpenAI model. Required for `openai`. | |
| `OPENAI_BASE_URL` | Base URL of an OpenAI-compatible service. Optional. | OpenAI's |
| `GOOGLE_API_KEY` | Gemini API key, from Google AI Studio. | |
| `GEMINI_MODEL` | Gemini model. | `gemini-3.5-flash-lite` |

**RSS and MCP**

| Variable | Description | Default |
|---|---|---|
| `RSS_FEEDS_JSON` | The feeds the server may read, as a JSON array on one line. Each feed has a unique lowercase `id`, a `name`, and an `https://` `url`. These are the only network sources the server uses; it rejects any other URL. | The CISA Alerts and GitHub Blog feeds |
| `RSS_CACHE_DATABASE` | The SQLite file the server caches articles in. It is created when missing. | `data/rss_cache.db` |
| `RSS_MCP_URL` | The URL the workers reach the MCP server at. It must match `MCP_HOST` and `MCP_PORT`. | `http://127.0.0.1:8013/mcp` |
| `MCP_HOST` | Interface the MCP server listens on. Use `0.0.0.0` only when another trusted machine or container must reach it. | `127.0.0.1` |
| `MCP_PORT` | Port the MCP server listens on. | `8013` |
| `MCP_TOOL_TIMEOUT_SECONDS` | Timeout for each request from a worker to the MCP server. | `30` |

The default feeds, as `.env.example` has them:

```dotenv
RSS_FEEDS_JSON=[{"id":"cisa","name":"CISA Alerts","url":"https://www.cisa.gov/cybersecurity-advisories/all.xml"},{"id":"github","name":"GitHub Blog","url":"https://github.blog/feed/"}]
```

To add a feed, find its URL on the publisher's site (usually behind an RSS icon or a "Subscribe" link), check that it opens as XML, and add it to the array.

**AgentDNA**

| Variable | Description | Default |
|---|---|---|
| `AGENTDNA_API_KEY` | Your AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io). Required. | |
| `PROVENANCE_LAYER_URL` | The AgentDNA Provenance Layer URL. | `https://chain-connector-2.rubix.net` |
| `ADMIN_SERVER_URL` | The AgentDNA admin server, which the MCP server asks whether every actor in the incoming workflow is whitelisted. Required. | |
| `USER_NAME` | Name of the user identity that signs requests and records results. | `user_123` |
| `RSS_ORCHESTRATOR_AGENT_NAME` | Name of the orchestrator's identity. | `RSS Orchestrator Agent` |
| `RSS_SECURITY_AGENT_NAME` | Name of the security worker's identity. | `RSS Security Agent` |
| `RSS_TECHNOLOGY_AGENT_NAME` | Name of the technology worker's identity. | `RSS Technology Agent` |
| `CBAC_URL` | The CBAC decision service the MCP server asks whether each tool call fits the user's intent. Leave empty for the default (production) service. | |

**Authentication evidence (optional)**

`AGENTDNA_AUTH_EVIDENCE`, `AGENTDNA_AUTH_EVIDENCE_FILE` and `AGENTDNA_FINGERPRINT_KEY` switch on AgentDNA's authentication evidence, which is off by default. See [Authentication evidence](../../README.md#authentication-evidence) in the main README.

## Run

From this directory, start the MCP server, then run the agents in a second terminal:

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 manual.py "List the configured RSS feeds and show their latest security and technology articles."
```

`manual.py` prints both workers' reports and the orchestrator's synthesis. Add `--json` after the prompt to print the structured execution event instead. On Windows, use `python` instead of `python3`.

### Example prompts

```sh
# Security and technology news across the configured feeds
python3 manual.py "List the configured RSS feeds and show their latest security and technology articles."

# Vulnerabilities and security advisories
python3 manual.py "List the configured RSS feeds and show their latest vulnerability and security advisory articles."

# AI, developer tooling and cloud
python3 manual.py "List the configured RSS feeds and show their latest articles about AI, developer tools and cloud platforms."
```

Each prompt asks for what the workers' tools do: list the configured feeds, then fetch their latest articles. CBAC judges every tool call against the user's prompt, and its check is probabilistic: a prompt that asks for something the tools don't do directly, such as a briefing on implications, can have the first call (`rss_list_feeds`) refused as drifting from the user's intent. The workers still analyse what they find, and the orchestrator still writes a synthesis.

### Automated

`automated.py` runs continuously until you stop it (Ctrl+C): each run picks one of ten built-in prompts at random, prints the JSON execution event, and records the chain.

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 automated.py
```

`scripts/run.sh` (and `scripts/run.ps1` / `scripts/run.cmd` on Windows) start the MCP server and run `automated.py`; stopping it stops the server too.

### MCP v2 SDK server

`mcp_server_mcp2.py` is the same RSS server on the MCP Python SDK v2 (`mcp.server.MCPServer`) instead of FastMCP, with AgentDNA's middleware from `agentdna.mcp.server.base`. Run it instead of `mcp_server.py`, not alongside it: both use the same port.

It needs `mcp>=2`, which `requirements.txt` doesn't install (it pins `mcp` 1.x for FastMCP 3), and AgentDNA's middleware module also needs FastMCP 4, the version that goes with `mcp` 2. Run it from an environment of its own:

```sh
python3 -m venv .venv-mcp2
source ./.venv-mcp2/bin/activate
pip install "mcp>=2" "fastmcp>=4,<5" feedparser python-dotenv cbac -e ../../../agentdna

python3 mcp_server_mcp2.py
```

The agents run from the main environment as before, and reach either server at `RSS_MCP_URL`.

## MCP tools

`rss_list_feeds`, `rss_get_latest_articles`, `rss_search_articles` and `rss_get_article`. The server only reads configured feeds, bounds result counts and queries, gives articles server-generated IDs, and never accepts a URL from a prompt or an agent. Article text is untrusted data.

The agents' skills and restrictions are in [agents/rss_orchestrator_agent/SKILLS.md](agents/rss_orchestrator_agent/SKILLS.md), [agents/rss_security_agent/SKILLS.md](agents/rss_security_agent/SKILLS.md) and [agents/rss_technology_agent/SKILLS.md](agents/rss_technology_agent/SKILLS.md); each is also the policy registered with that agent's AgentDNA identity.

## Tests

From this directory: `python -m pytest tests -q`. The tests check that only configured feeds are listed, and that invalid article IDs and result limits are rejected. They create the AgentDNA identities, so `.env` must be set first.
