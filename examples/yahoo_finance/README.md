# Yahoo Finance MCP Agent (signed by AgentDNA)

A Streamlit agent that pulls live quotes and price history from Yahoo Finance via a local **MCP server** (`server.py`). Every host → MCP round-trip is signed and verified by AgentDNA; each verified turn is written to an audit-log NFT on Rubix.

> ⚠️ Yahoo Finance has no official public API. The server uses `yfinance` (web-scraped). Data is best-effort.

## Prerequisites

- Python **3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A **Google AI Studio API key** ([get one](https://aistudio.google.com/app/apikey))
- An [AgentDNA API key](https://agentdna.io/beta) (sign up for the Beta)

## 1. Configure `.env`

```bash
cp .env.sample .env
```

Then edit `.env` and fill in the values:

```env
AGENTDNA_API_KEY=...
GEMINI_API_KEY=...            # works with GOOGLE_API_KEY too
HOST_AGENT_NAME=YahooHost
MCP_TOOL_NAME=YahooFinanceMCP
```

## 2. Run

```bash
uv run streamlit run app.py
```

That installs deps into `.venv/` on first run and opens the UI at <http://localhost:8501>. `server.py` is spawned automatically over stdio.

## What you can ask

- `What is the current price of AAPL?`
- `Show me the last 5 days of price history for TSLA.`
- `Get the latest stock price for MSFT.`

The host LLM (Gemini 2.5 Flash) decides whether to call `get_quote(symbol)` or `get_history(symbol, period)` and fills in the args.

## Trust layer at a glance

- **Host** (`app.py`) signs the tool call with `dna.envelope(host_msg)` and verifies the reply with `dna.verify_reply(...)`. Each verified turn writes one record to the host's audit-log NFT.
- **Server** (`server.py`) is a pure remote (`enable_nft=False`) — it verifies the host envelope with `dna.verify_request(...)`, runs the `yfinance` call, then signs the reply with `dna.sign_response(payload, ctx=ctx)`.
- The sidebar's **History Records** button fetches chain history via `dna.history()` and renders it as a foldable JSON tree.

## Troubleshooting

**`API Key not found / API_KEY_INVALID`** — your Gemini key is dead/wrong project. Regenerate at <https://aistudio.google.com/app/apikey>. The app accepts either `GEMINI_API_KEY` or `GOOGLE_API_KEY`.

**`ModuleNotFoundError: No module named 'yfinance'`** — you're running with the wrong Python (likely system / anaconda). Always launch via `uv run streamlit run app.py` from this directory so the spawned `server.py` inherits the venv's interpreter.

**`Decoded key is not an uncompressed secp256k1 key`** — older `rubix-py 0.7.x` keystore. Move it aside:
```bash
mv ~/.agentdna/account/$HOST_AGENT_NAME ~/.agentdna/account/$HOST_AGENT_NAME.compressed-bak
mv ~/.agentdna/account/$MCP_TOOL_NAME   ~/.agentdna/account/$MCP_TOOL_NAME.compressed-bak
```
Next launch regenerates the keys in the new format.
