# Jira MCP Agent (signed by AgentDNA)

A Streamlit agent that talks to Jira Cloud through a local **MCP server** (`server.py`). Every host → MCP round-trip is signed and verified by AgentDNA; each verified turn is written to an audit-log NFT on Rubix.

> **Note:** This example still uses the legacy `dna.build()` / `dna.handle()` API. The newer ergonomic methods (`dna.envelope` / `dna.verify_reply` / `dna.verify_request` / `dna.sign_response`) are used by the other examples (`google_sheets`, `github`, `yahoo_finance`, `gemini_research_assistant`, `multi_agent_system`). Migrating this one is on the todo list.

## Tools exposed by the MCP server

| Tool | What it does |
|------|-------------|
| `search_issues` | JQL search via Jira v3 `/search/jql` |
| `get_issue` | Retrieve issue details |
| `create_issue` | Create a new issue (ADF description support) |
| `add_comment` | Add a comment to an issue |
| `transition_issue` | Move an issue across the workflow |

## Prerequisites

- Python **3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A **Jira Cloud account** + [API token](https://id.atlassian.com/manage-profile/security/api-tokens)
- A **Google AI Studio API key** ([get one](https://aistudio.google.com/app/apikey))
- An [AgentDNA API key](https://agentdna.io/beta) (sign up for the Beta)

## 1. Configure `.env`

```bash
cp .env.sample .env
```

Then edit `.env` and fill in the values:

```env
AGENTDNA_API_KEY=...
GEMINI_API_KEY=...
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=...
```

## 2. Run

```bash
uv run streamlit run app.py
```

That installs deps into `.venv/` on first run and opens the UI at <http://localhost:8501>. `server.py` is spawned automatically over stdio.

## What you can ask

- `List my open issues`
- `Create a new task in KAN. Summary: Add login button. Description: Implement UI button for login`
- `Add a comment to KAN-12 saying "Reviewed and approved"`
- `Move KAN-12 to In Progress`

The host LLM (Gemini 2.5 Flash) decides which tool to call and fills in the args.

## Trust layer flow

```
User → Streamlit UI → Gemini (decides tool)
                       ↓
                       dna.build(...)            ← signs host envelope
                       ↓
              MCP call_tool(args + dna_envelope)
                       ↓
              MCP Server: dna.handle(raw_text=…) ← verify host
              MCP Server: Jira REST call
              MCP Server: dna.build(response=…) ← signs reply
                       ↓
              Host: dna.handle(resp_parts=…)    ← verify + NFT write
                       ↓
                  Final Gemini answer → UI
```

The sidebar's **History Records** button reads back the chain history. Each turn is one immutable record signed by the host's DID.

## Troubleshooting

**`401 / 403` from Jira** — `JIRA_EMAIL` + `JIRA_API_TOKEN` combination wrong, or the token doesn't have access to the project you're referencing.

**`API_KEY_INVALID`** — your Gemini key is dead. Regenerate at <https://aistudio.google.com/app/apikey>.

**`Decoded key is not an uncompressed secp256k1 key`** — older `rubix-py 0.7.x` keystore. Move it aside:
```bash
mv ~/.agentdna/account/jira_host   ~/.agentdna/account/jira_host.compressed-bak
mv ~/.agentdna/account/jira_server ~/.agentdna/account/jira_server.compressed-bak
```
Next launch regenerates the keys.

## License

MIT
