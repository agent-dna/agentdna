# GitHub Repository Agent

An independent LangGraph ReAct workload that analyses one configured GitHub repository through a local FastMCP server. The agent has no direct GitHub API access: the MCP server owns the credential and exposes read-only capabilities only.

## Architecture

`scripts/run.*` starts the FastMCP streamable-HTTP server and the AgentDNA MCP Gateway in front of it, then starts `github-repository-agent`. The named agent discovers the server's tools using `langchain_mcp_adapters` and executes a LangGraph ReAct loop. Repository text is explicitly treated as untrusted data.

```text
github-repository-agent -> langchain_mcp_adapters -> AgentDNA MCP Gateway -> FastMCP -> GitHub REST API
```

The agent never talks to the MCP server directly and its MCP client carries no AgentDNA patch. For each execution the agent opens a run on the gateway with the IntentWorkflow it received, points `langchain_mcp_adapters` at the run's URL, and closes the run to collect the workflows its tool calls produced. The gateway signs every tool call as the agent (`AGENTDNA_AGENT_NAME`), verifies the MCP server's signed response, and keeps the run's chain of custody.

## Prerequisites

- Python 3.11 or later
- An LLM backend: Ollama, OpenAI, or Gemini
- A GitHub token with read access to the configured repository

## Setup

1. Create and activate a virtual environment:

```
python3 -m venv .venv
chmod +x .venv/bin/activate
source .venv/bin/activate
```

2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and set `GITHUB_TOKEN`, `GITHUB_REPOSITORY_OWNER`, and `GITHUB_REPOSITORY_NAME`.
4. Configure `LLM_BACKEND` and the corresponding provider settings.
5. Set the AgentDNA values: `AGENTDNA_API_KEY`, `AGENTDNA_AGENT_NAME`, `USER_NAME` and, optionally, `AGENTDNA_PROVENANCE_LAYER_URL`. The gateway reads the same `.env`, so it signs as the same agent.

For OpenAI set `LLM_BACKEND=openai`, `OPENAI_API_KEY`, and `OPENAI_MODEL`; `OPENAI_BASE_URL` is optional. For Gemini set `LLM_BACKEND=gemini`, `GOOGLE_API_KEY`, and `GEMINI_MODEL`. For Ollama set `LLM_BACKEND=ollama`, then ensure `OLLAMA_MODEL` is pulled and `OLLAMA_HOST` is reachable.

## Sample Prompts

Set `GITHUB_ANALYSIS_TASK` to one of the following before an execution:

- `Summarise commits, open issues, and pull requests from the last 30 days. Identify delivery risks and cite the supporting repository evidence.`
- `Review the repository's top-level documentation and recent changes. Highlight security-sensitive areas that deserve a maintainer review.`
- `Analyse unresolved issues and open pull requests for stalled work, duplicated effort, and dependencies that could block the next release.`

## Run Modes

### Manual

From this project directory, start the MCP server and the gateway, then provide a task directly to the Python CLI:

```sh
# Terminal 1: the GitHub MCP server
python3 mcp_server.py

# Terminal 2: the AgentDNA MCP Gateway, forwarding to AGENTDNA_GATEWAY_UPSTREAM_URL
python3 -m agentdna.mcp.gateway

# Terminal 3: the agent
python3 manual.py "Your prompt"
```

On Windows, replace `python3` with `python`.

### Automated

`automated.py` randomly selects one of ten built-in repository-analysis prompts for each execution. `scripts/run.sh` (or `scripts/run.cmd` on Windows) starts the MCP server and the gateway, waits for the gateway, and runs that automated entrypoint. To run the pieces yourself:

```
# Terminal 1
python3 mcp_server.py

# Terminal 2
python3 -m agentdna.mcp.gateway

# Terminal 3
python3 automated.py
```

## MCP Tools

`repository_metadata`, `directory_listing`, `file_contents`, `commits`, `issues`, and `pull_requests` are server-side, validated, read-only tools. The configured repository is the only resource the server permits.

## Security And Observability

The GitHub token remains in the MCP server process and never enters an LLM prompt or tool argument. Every tool call is signed by the gateway and verified by the MCP server's AgentDNA middleware; a tool result is only handed to the agent once the server's signed response has been verified by the gateway. The agent's skills and restrictions are in [agents/github_repository_agent/SKILLS.md](agents/github_repository_agent/SKILLS.md). Each run emits a JSON event with a stable agent identity and a new execution ID.

## Tests

Run `python -m pytest tests -q` from this project directory.
