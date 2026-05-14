# GitHub MCP Agent (signed by AgentDNA)

A Streamlit agent that calls a local **MCP server** (`server.py`) which talks to the GitHub API. Every host → MCP round-trip is signed and verified by AgentDNA; each verified turn is written to an audit-log NFT on Rubix.

The server hard-codes a target repo at the top of `server.py`:

```python
REPO_OWNER = "SynapzeCore"
REPO_NAME  = "sample-repo"
BASE_BRANCH = "main"
```

Change those to point at any repo your `GITHUB_TOKEN` has access to.

## Prerequisites

- Python **3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A **GitHub Personal Access Token** with `repo` scope ([create one](https://github.com/settings/tokens))
- A **Google AI Studio API key** ([get one](https://aistudio.google.com/app/apikey))
- An [AgentDNA API key](https://agentdna.io/beta) (sign up for the Beta)

## 1. Configure `.env`

```bash
cp .env.sample .env
```

Then edit `.env` and fill in the values:

```env
AGENTDNA_API_KEY=...
GEMINI_API_KEY=...                  # works with GOOGLE_API_KEY too
GITHUB_TOKEN=ghp_...
HOST_AGENT_NAME=GitHub_Host_Agent
MCP_TOOL_NAME=GitHub_Tool
```

## 2. Run

```bash
uv run streamlit run app.py
```

That installs deps into `.venv/` on first run and opens the UI at <http://localhost:8501>. `server.py` is spawned automatically over stdio.

## What you can ask

- `Create an issue with title "Bug: Login fails" and description "Users can't log in via OAuth."`
- `Open a pull request from feature/login titled "Add login button" with description "Initial PR for login UI"`

The host LLM (Gemini 2.5 Flash) decides whether to call `create_issue` or `create_pull_request` and fills in the args.

## Trust layer at a glance

- **Host** (`app.py`) signs the tool call with `dna.build(host_msg)` and verifies the reply with `dna.handle(reply, original=env)`. Each verified turn writes one record to the host's audit-log NFT.
- **Server** (`server.py`) is a pure remote (`enable_nft=False`) — it verifies the host envelope with `dna.handle(envelope)`, hits the GitHub REST API, then signs the reply with `dna.build(payload, ctx=ctx)`.
- The sidebar's **History Records** button fetches chain history via `dna.history()` and renders it as a foldable JSON tree.

## Troubleshooting

**`API Key not found / API_KEY_INVALID`** — your Gemini key is dead. Regenerate at <https://aistudio.google.com/app/apikey> or set `GOOGLE_API_KEY` (the app accepts either).

**`Decoded key is not an uncompressed secp256k1 key`** — older `rubix-py 0.7.x` keystore. Move it aside:
```bash
mv ~/.agentdna/account/$HOST_AGENT_NAME ~/.agentdna/account/$HOST_AGENT_NAME.compressed-bak
mv ~/.agentdna/account/$MCP_TOOL_NAME   ~/.agentdna/account/$MCP_TOOL_NAME.compressed-bak
```
Next launch will regenerate the keys.

**GitHub 401 / 403** — `GITHUB_TOKEN` doesn't have access to the repo named in `server.py`. Either swap to a repo the token can write to, or generate a token with `repo` scope for the right account.
