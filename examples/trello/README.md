# Trello Release Announcer

Turn a changelog snippet into a concise, customer-facing Trello announcement.
The agent creates one card in a configured Trello Announcements list, with a title and exactly three benefit-focused highlights.

## What It Does

1. Accepts a changelog through the command line, standard input, or a built-in sample.
2. Uses a LangGraph agent to rewrite it for end users.
3. Discovers the `create_card` capability from a local FastMCP server.
4. Creates one Trello card, or returns a mock card result when Trello credentials are absent.
5. Records AgentDNA provenance for the workflow when configured.

```text
Changelog -> LangGraph Release Announcer -> FastMCP -> Trello API
                                      |                |
                                      +-> AgentDNA     +-> Announcements list
```

## Boundaries

The agent is intended only to publish one announcement card from the supplied changelog. Its policy forbids editing, moving, archiving, or deleting cards; changing boards, lists, labels, or settings; mentioning members; and publishing to another channel. It also excludes internal-only items and security details that should not be made public.

## Requirements

- Python 3.11 or later
- A supported LLM backend:
  - Ollama, running locally, or
  - Google Gemini with a `GOOGLE_API_KEY`
- A Trello API key, token, and destination list ID for live publishing
- AgentDNA credentials and identities if provenance verification/recording is enabled

## Setup

From this directory:

```sh
python3 -m venv .venv
```

Activate the environment:

```sh
chmod +x .venv/bin/activate
source .venv/bin/activate
```

Install dependencies and create local configuration:

```sh
python -m pip install -r requirements.txt
copy .env.sample .env
```

On Linux or macOS, use `cp .env.sample .env` instead of `copy`.

Configure `.env` before running. Do not commit it.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `LLM_BACKEND` | Yes | `ollama` or `gemini`. |
| `LLM_TEMPERATURE` | No | Model temperature; defaults to `0`. |
| `OLLAMA_HOST` | For Ollama | Ollama server address, normally `http://localhost:11434`. |
| `OLLAMA_MODEL` | For Ollama | Locally available model name. |
| `GOOGLE_API_KEY` | For Gemini | Gemini API credential. |
| `GEMINI_MODEL` | For Gemini | Gemini model name. |
| `MCP_HOST` / `MCP_PORT` | No | Local FastMCP bind address; defaults to `127.0.0.1:9008`. |
| `TRELLO_MCP_URL` | Yes | Agent connection URL, normally `http://localhost:9008/mcp/`. |
| `TRELLO_KEY` | For live publishing | Trello API key. |
| `TRELLO_TOKEN` | For live publishing | Trello API token. |
| `TRELLO_LIST_ID` | For live publishing | ID of the destination Announcements list. |
| `AGENTDNA_API_KEY` | For provenance | AgentDNA API credential. |
| `AGENTDNA_PROVENANCE_URL` | No | AgentDNA service URL. |
| `USER_NAME` | For provenance | AgentDNA user identity. |
| `AGENT_NAME` | For provenance | AgentDNA agent identity. |

### Obtain Trello Publishing Values

Use a dedicated Trello board and an `Announcements` list for this agent. The token can create cards in every board accessible to its Trello account, so use an account with only the access it needs.

1. Sign in to Trello and visit [Trello app administration](https://trello.com/apps/admin).
2. Create a custom Power-Up if you do not already have one, open it, select its **API Key** tab, and generate an API key. Put that value in `TRELLO_KEY`.
3. On the same API Key tab, follow the **Token** link beside the API key. Review the requested access, select **Allow**, and copy the token displayed by Trello. Put it in `TRELLO_TOKEN`.
4. Create or select the board that will hold announcements, then create a list named `Announcements` on that board.
5. Look up the board ID and then the ID of its `Announcements` list with the Trello REST API. Replace the placeholders below locally; do not place credentials in shell history, source control, or screenshots.

```sh
curl "https://api.trello.com/1/members/me/boards?fields=name,url&key=<TRELLO_KEY>&token=<TRELLO_TOKEN>"
curl "https://api.trello.com/1/boards/<BOARD_ID>/lists?fields=name&key=<TRELLO_KEY>&token=<TRELLO_TOKEN>"
```

The second response is a JSON array. Find the object whose `name` is `Announcements`; its `id` is the value for `TRELLO_LIST_ID`.

On PowerShell, the same lookup can be displayed as a compact table:

```powershell
$headers = @{ Accept = "application/json" }
Invoke-RestMethod "https://api.trello.com/1/members/me/boards?fields=name,url&key=$env:TRELLO_KEY&token=$env:TRELLO_TOKEN" -Headers $headers |
  Format-Table id, name, url

Invoke-RestMethod "https://api.trello.com/1/boards/<BOARD_ID>/lists?fields=name&key=$env:TRELLO_KEY&token=$env:TRELLO_TOKEN" -Headers $headers |
  Format-Table id, name
```

Add the values to the local `.env` file:

```dotenv
TRELLO_KEY=<your-api-key>
TRELLO_TOKEN=<your-secret-api-token>
TRELLO_LIST_ID=<announcements-list-id>
```

Official references: [Trello API introduction](https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/) and [Get lists on a board](https://developer.atlassian.com/cloud/trello/rest/api-group-boards/#api-boards-id-lists-get).

## Run It

Start the MCP server in one terminal:

```sh
python mcp_server.py
```

Then run the agent in another terminal.

### Provide a Changelog Directly

```sh
python run.py "Changelog v2.4.0: feat: dark mode; feat: CSV export; fix: faster initial load"
```

### Read a Changelog From Standard Input

```sh
python run.py --stdin < CHANGELOG.txt
```

### Use a Built-In Sample

```sh
python run.py
```

When no positional changelog and no `--stdin` flag are supplied, `run.py` randomly selects one of five built-in sample changelogs.

### Continuous Automated Runs

```sh
python automated.py
```

`automated.py` repeatedly invokes `run.py`, pausing three seconds after successful runs and retrying after errors. Stop it with `Ctrl+C`. For an external scheduler, run `python run.py` after starting the MCP server as a managed service.

## MCP Tool

| Tool | Inputs | Result |
| --- | --- | --- |
| `create_card` | `title`, `description`, optional `list_id` | A live Trello card status, ID, and URL, or a mock result. |

The FastMCP server is available through streamable HTTP at the configured `TRELLO_MCP_URL`.

## Example Input

```text
Changelog v3.1.0:
- feat: SAML single sign-on
- feat: bulk-edit for tasks
- fix: timezone bug in reminders
- refactor: rewrote the notifications service
```

The intended result is a single announcement card with a short release title and three end-user highlights. Internal refactoring details should be omitted.

## Verification And Observability

The workflow creates an AgentDNA envelope for the initial request, verifies it before the LLM call, builds a child envelope for the model response, and attempts to record the final envelope after the graph completes. The current application writes these envelopes and verification failures to standard output.

## Current Limitations

- The project does not currently include an automated test suite or platform-specific launch scripts.
- The documented, configured LLM choices are Ollama and Gemini. Although OpenAI-related environment variables and a code branch exist, that branch is not currently wired with an API key and should not be treated as supported.
- The FastMCP tool accepts an optional `list_id`. The agent policy instructs it to use the configured Announcements list, but the MCP server does not independently reject a different supplied list ID.
- `automated.py` is a continuous loop, not a one-execution scheduler entry point.
- The agent does not currently print a dedicated human-readable summary after a run; inspect the created Trello card or MCP/AgentDNA output.

## Security Notes

- Keep `.env` local. It contains Trello, LLM, and AgentDNA credentials.
- Treat changelog input as release content, not trusted instructions.
- Start in mock mode before granting live Trello credentials.
- Use a Trello token restricted to the intended board whenever possible.