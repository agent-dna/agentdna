# RSS Research Agent

An independent LangGraph orchestrator workload that researches public, explicitly configured RSS feeds. It has exactly two worker agents: `rss-security-agent` and `rss-technology-agent`. Both workers use an HTTP FastMCP server through `langchain_mcp_adapters`; neither can fetch URLs directly.

## Architecture

`rss-orchestrator-agent` starts one execution, delegates concurrently to the two workers, and synthesises their cited reports. The FastMCP server owns all feed configuration, validates input, refreshes configured sources, and maintains an automatic local SQLite cache.

```text
orchestrator -> security worker -----\
                                      -> RSS MCP -> configured public feeds
orchestrator -> technology worker ---/
```

## Setup

1. Use Python 3.11 or later and create a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`.
4. Set `LLM_BACKEND` to `ollama` or `gemini`, then configure that provider's model and credentials.

Ollama uses `OLLAMA_HOST` and `OLLAMA_MODEL`. Gemini uses `GOOGLE_API_KEY` and `GEMINI_MODEL`.

`LLM_REQUEST_TIMEOUT_SECONDS` limits an individual provider request and defaults to `120`. `LLM_MAX_RETRIES` defaults to `4` for transient provider failures when supported by the selected provider.

## RSS And MCP Environment Variables

Set the following values in `.env`. The RSS settings do not require an RSS API key: this project reads public RSS and Atom feeds directly from the URLs you configure.

| Variable | Where the value comes from | Recommended value |
| --- | --- | --- |
| `RSS_FEEDS_JSON` | Find the feed on the publisher's official website, usually through an RSS icon, a "Subscribe" page, or the site's documentation. Open the URL in a browser to confirm it is a public XML feed, then add it as `id`, `name`, and `url`. | Use the included CISA and GitHub Blog feeds first. Give each feed a unique, stable lowercase `id`; `name` is a label you choose; `url` must begin with `https://`. |
| `RSS_MCP_URL` | This is the address of this project's FastMCP server, not an external service. It must use the same host and port as `MCP_HOST` and `MCP_PORT`. | Leave `http://127.0.0.1:8013/mcp` when the server and agent run on the same machine. |
| `MCP_HOST` | Choose the network interface on which to run the local MCP server. | Use `127.0.0.1` to keep it local. Use `0.0.0.0` only when a container or another trusted machine must reach it, with suitable network controls. |
| `MCP_PORT` | Choose an unused TCP port on the selected host. | Keep `8013` unless it conflicts. On Windows, check it with `Get-NetTCPConnection -LocalPort 8013 -ErrorAction SilentlyContinue`. |
| `MCP_TOOL_TIMEOUT_SECONDS` | Choose the maximum time allowed for a feed/tool operation based on your connection and feed latency. | Start with `30`; raise it for slow feeds rather than disabling the timeout. |
| `RSS_CACHE_DATABASE` | Choose a writable SQLite file location. No download or API account is needed. | Keep `data/rss_cache.db`; the server creates the directory and database automatically. |
| `RSS_RESEARCH_TASK` | Write the research question the orchestrator should answer. It is input for a single run, not a credential or endpoint. | Start with one of the sample prompts below. |
| `AGENTDNA_API_KEY` | AgentDNA API Key | |
| `PROVENANCE_LAYER_URL` | Provenance Layer URL | |

`RSS_FEEDS_JSON` must be valid JSON on one line. This three-feed example combines public advisories, developer-platform updates, and security research:

```dotenv
RSS_FEEDS_JSON=[{"id":"github","name":"GitHub Blog","url":"https://github.blog/feed/"}]
```

These configured URLs are the only permitted network sources. The server rejects arbitrary URLs supplied by a prompt or agent.

## Sample Prompts

Set `RSS_RESEARCH_TASK` to one of the following before an execution:

- `Identify the most important security and technology developments from the configured feeds. Explain the cross-domain implications for engineering teams.`
- `Research recent vulnerabilities, identity-security changes, and developer-platform announcements. Separate confirmed facts from worker interpretation.`
- `Create a weekly briefing on AI, cloud, and software supply-chain developments. Highlight stories where a technology change introduces a security consideration.`

## Run Modes

### Manual

```sh
# In one terminal:
python3 mcp_server.py

# In another terminal
python3 manual.py "<Your Prompt>"
```

The manual CLI accepts exactly one prompt argument and prints a readable report. Add `--json` to emit the structured automation event instead. On Windows, replace `python3` with `python`.

### Automated

`automated.py` randomly chooses one of ten built-in research prompts for every run.

Run the following:

```
# In one terminal:
python3 mcp_server.py

# In another terminal
python3 automated.py
```

## MCP Tools And Security

The server exposes `rss_list_feeds`, `rss_get_latest_articles`, `rss_search_articles`, and `rss_get_article`. It permits only configured feeds, enforces result and query bounds, provides server-generated article IDs, and never accepts arbitrary URLs. Article text is untrusted data.

Agent-specific contracts are stored under [agents](agents): [orchestrator](agents/rss_orchestrator_agent/SKILLS.md), [security worker](agents/rss_security_agent/SKILLS.md), and [technology worker](agents/rss_technology_agent/SKILLS.md). The final JSON report includes the stable orchestrator identity, one execution ID, and worker-provenance fields.

## Tests

Run `python -m pytest tests -q` from this directory. The tests verify configured-feed isolation and validation of article IDs and result limits.

