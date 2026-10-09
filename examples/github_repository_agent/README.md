# GitHub Repository Agent

An independent LangGraph ReAct workload that analyses one configured GitHub repository through a local FastMCP server. The agent has no direct GitHub API access: the MCP server owns the credential and exposes read-only capabilities only.

## Architecture

`scripts/run.*` starts the FastMCP streamable-HTTP server, then starts `github-repository-agent`. The named agent discovers the server's tools using `langchain_mcp_adapters` and executes a LangGraph ReAct loop. Repository text is explicitly treated as untrusted data.

```text
github-repository-agent -> langchain_mcp_adapters -> FastMCP -> GitHub REST API
```

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

For OpenAI set `LLM_BACKEND=openai`, `OPENAI_API_KEY`, and `OPENAI_MODEL`; `OPENAI_BASE_URL` is optional. For Gemini set `LLM_BACKEND=gemini`, `GOOGLE_API_KEY`, and `GEMINI_MODEL`. For Ollama set `LLM_BACKEND=ollama`, then ensure `OLLAMA_MODEL` is pulled and `OLLAMA_HOST` is reachable.

## Sample Prompts

Set `GITHUB_ANALYSIS_TASK` to one of the following before an execution:

- `Summarise commits, open issues, and pull requests from the last 30 days. Identify delivery risks and cite the supporting repository evidence.`
- `Review the repository's top-level documentation and recent changes. Highlight security-sensitive areas that deserve a maintainer review.`
- `Analyse unresolved issues and open pull requests for stalled work, duplicated effort, and dependencies that could block the next release.`

## Run Modes

### Manual

From the `examples` parent directory, start the MCP server and provide a task directly to the Python CLI:

```sh
python3 mcp_server.py

python3 manual.py "Your prompt"
```

On Windows, replace `python3` with `python`.

### Automated

`automated.py` randomly selects one of ten built-in repository-analysis prompts for each execution. The scheduled launchers start the MCP server, run that automated entrypoint, emit JSON output, and exit:

```
# In one terminal run the MCP server
python3 mcp_server.py

# In another terminal
python3 automated.py
```

## MCP Tools

`repository_metadata`, `directory_listing`, `file_contents`, `commits`, `issues`, and `pull_requests` are server-side, validated, read-only tools. The configured repository is the only resource the server permits.

## Security And Observability

The GitHub token remains in the MCP server process and never enters an LLM prompt or tool argument. The agent's skills and restrictions are in [agents/github_repository_agent/SKILLS.md](agents/github_repository_agent/SKILLS.md). Each run emits a JSON event with a stable agent identity and a new execution ID.

## Tests

Run `python -m pytest tests -q` from this project directory.
