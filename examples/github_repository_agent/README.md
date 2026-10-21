# GitHub Repository Agent

A LangGraph ReAct agent that analyses one GitHub repository through a local FastMCP server. The agent has no direct GitHub API access: the MCP server holds the GitHub token and exposes read-only tools.

## Architecture

```text
github-repository-agent -> langchain_mcp_adapters -> AgentDNA Provenance Relay -> FastMCP + AgentDNA middleware -> GitHub REST API
```

- `manual.py` / `automated.py`: the user signs the request, the agent runs, and the user records the finished chain on the provenance layer.
- `agents/github_agent/agent.py`: the agent. It opens a Provenance Relay session on the user's workflow, and points `langchain_mcp_adapters` at the session's URL, so every tool call is signed as the agent.
- `mcp_server.py`: the GitHub MCP server, behind `AgentDNAMCPMiddleware`. Every tool call must come from a whitelisted agent, pass CoCA verification, and be allowed by CBAC.

```py
PROVENANCE_RELAY = ProvenanceRelay({"github": settings.github_mcp_url})

async with PROVENANCE_RELAY.session(GITHUB_AGENT, adna_workflow) as session:   # verifies the incoming workflow
    tools = await load_tools(session.url("github"))
    result = await workflow.ainvoke(...)

# session.workflows: the user's workflow plus every tool call
reply_workflow = GITHUB_AGENT.build(payload=..., previous_workflows=session.workflows)
```

If the MCP server refuses a tool call (whitelist, CoCA or CBAC), it records the refusal and the run stops with an error saying the run's chain has ended, along with the reason. Nothing is recorded on the agent's side.

## Prerequisites

- Python 3.11 or 3.12
- An LLM: Ollama, OpenAI (or an OpenAI-compatible service), or Gemini
- A GitHub token with read access to the repository
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

### Environment variables

**LLM**

| Variable | Description | Default |
|---|---|---|
| `LLM_BACKEND` | `ollama`, `openai` or `gemini`. | `ollama` |
| `LLM_TEMPERATURE` | Sampling temperature. | `0` |
| `OLLAMA_HOST` | Ollama server URL. | `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | Ollama model; pull it first (`ollama pull <model>`). | `llama3.1` |
| `OPENAI_API_KEY` | OpenAI API key. | |
| `OPENAI_MODEL` | OpenAI model. | `gpt-4o-mini` |
| `OPENAI_BASE_URL` | Base URL of an OpenAI-compatible service. Optional. | OpenAI's |
| `GOOGLE_API_KEY` | Gemini API key, from Google AI Studio. | |
| `GEMINI_MODEL` | Gemini model. | `gemini-3.5-flash-lite` |

**GitHub and MCP**

| Variable | Description | Default |
|---|---|---|
| `GITHUB_TOKEN` | GitHub token with read access to the repository. Only the MCP server uses it. Required. | |
| `GITHUB_REPOSITORY_OWNER` | Owner of the repository to analyse. Required. | |
| `GITHUB_REPOSITORY_NAME` | Name of the repository to analyse. Required. | |
| `GITHUB_API_URL` | GitHub REST API URL; change it for GitHub Enterprise Server. | `https://api.github.com` |
| `GITHUB_MCP_URL` | The URL the agent reaches the MCP server at. It must match `MCP_HOST` and `MCP_PORT`. | `http://127.0.0.1:8011/mcp` |
| `MCP_HOST` | Interface the MCP server listens on. | `127.0.0.1` |
| `MCP_PORT` | Port the MCP server listens on. | `8011` |
| `MCP_TOOL_TIMEOUT_SECONDS` | Timeout for each MCP request from the agent, and for the server's GitHub API calls. | `30` |
| `GITHUB_ANALYSIS_TASK` | The task used when the prompt given to the agent is empty. | `Analyse recent repository activity, notable issues, pull requests, changes, and risks.` |

**AgentDNA**

| Variable | Description | Default |
|---|---|---|
| `AGENTDNA_API_KEY` | Your AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io). Required. | |
| `PROVENANCE_LAYER_URL` | The AgentDNA Provenance Layer URL. | `https://chain-connector-2.rubix.net` |
| `ADMIN_SERVER_URL` | The AgentDNA admin server, which the MCP server asks whether the agent is whitelisted. | `https://agentdna-admin.agentdna.io` |
| `USER_NAME` | Name of the user identity that signs requests and records results. Required. | |
| `GITHUB_AGENT_NAME` | Name of the agent's identity. Required. | |
| `CBAC_URL` | The CBAC decision service the MCP server asks whether each tool call fits the user's intent. Leave empty for the default (production) service. | |

**Authentication evidence (optional)**

`AGENTDNA_AUTH_EVIDENCE`, `AGENTDNA_AUTH_EVIDENCE_FILE` and `AGENTDNA_FINGERPRINT_KEY` switch on AgentDNA's authentication evidence, which is off by default. See [Authentication evidence](../../README.md#authentication-evidence) in the main README.

## Run

From this directory, start the MCP server, then run the agent in a second terminal:

```sh
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 manual.py "Review the repository's open issues and pull requests."
```

`manual.py` prints the analysis. Add `--json` after the prompt to print the structured execution event instead. On Windows, use `python` instead of `python3`.

### Example prompts

```sh
python3 manual.py "Review the repository's open issues and pull requests."
python3 manual.py "Review the repository's recent commits and summarise the changes."
python3 manual.py "Review the repository's top-level files and documentation."
```

CBAC judges every tool call against the user's prompt, and its check is probabilistic. A prompt that asks for what the tools do (read issues, pull requests, commits, files) is less likely to have a call refused as drifting from the user's intent than one that asks for a broad judgement.

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

`repository_metadata`, `directory_listing`, `file_contents`, `commits`, `issues` and `pull_requests`: read-only, validated tools over the configured repository only. The GitHub token stays in the MCP server process; it never reaches the LLM or a tool argument.

The agent's skills and restrictions are in [agents/github_agent/SKILLS.md](agents/github_agent/SKILLS.md), which is also the policy registered with its AgentDNA identity.

## Tests

From this directory: `python -m pytest tests -q`.
