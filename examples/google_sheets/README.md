# MCP Google Sheets Agent (signed by AgentDNA)

A Streamlit chat UI that talks to a local **MCP server** (`server.py`) which reads and writes tasks to a Google Sheet. Every host → MCP round-trip is signed and verified by AgentDNA; each verified turn is written to an audit-log NFT on Rubix.

## Prerequisites

- Python **3.11+** and [uv](https://docs.astral.sh/uv/getting-started/installation/)
- A Google **Service Account JSON** with the Google Sheets API enabled, placed at `credentials/service_account.json`
- A spreadsheet shared with the service account's `client_email`
- An [AgentDNA API key](https://agentdna.io/beta) (sign up for the Beta)

## 1. Configure `.env`

```bash
cp .env.sample .env
```

Then edit `.env` and fill in the values:

```env
AGENTDNA_API_KEY=...
CHAIN_URL=https://chain-connector-1.rubix.net
GOOGLE_APPLICATION_CREDENTIALS=credentials/service_account.json
GSHEETS_SPREADSHEET_ID=your_spreadsheet_id_here
GSHEETS_SHEET_NAME=Sheet1
AGENTDNA_REMOTE_NAME=GoogleSheetsMCP
```

The sheet's first row must be `id | title | status | owner | notes | created_at`. Start from an empty tab — the server will write the header for you on first use.

## 2. Run

```bash
uv run streamlit run app.py
```

That installs deps into a local `.venv/` on first run, then opens the UI at <http://localhost:8501>. `server.py` is spawned automatically over stdio.

## What you can ask

- `Add: finish report`
- `Show open tasks`
- `Mark <title> done`
- `Set owner of <title> to Alice`

## Trust layer at a glance

- **Host** (`app.py`) signs every tool call with `dna.build(host_msg)` and verifies the signed reply via `dna.handle(reply, original=env)`. Each verified turn writes one record to the host's audit-log NFT.
- **Server** (`server.py`) is a pure remote (`enable_nft=False`) — it verifies the host envelope with `dna.handle(envelope)`, does the sheet work, and signs the reply with `dna.build(payload, ctx=ctx)`.
- The sidebar's **History Records** button fetches the chain history via `dna.history()` and renders it as a foldable JSON tree.

## Troubleshooting

**`Missing AGENTDNA_API_KEY`** — set it in `.env`. Sign up at <https://agentdna.io/beta>.

**Google permission errors** — confirm the spreadsheet is shared with `client_email` from `credentials/service_account.json` and that the Google Sheets API is enabled in your GCP project.

**`Header row mismatch`** — point `GSHEETS_SHEET_NAME` at a fresh empty tab and the server will write the header itself.

**`Decoded key is not an uncompressed secp256k1 key`** — you have an older keystore from `rubix-py 0.7.x`. Move it aside:
```bash
mv ~/.agentdna/account/GoogleSheetsAgent ~/.agentdna/account/GoogleSheetsAgent.compressed-bak
mv ~/.agentdna/account/GoogleSheetsMCP   ~/.agentdna/account/GoogleSheetsMCP.compressed-bak
```
The next launch will regenerate keys in the new format. Note: this creates a fresh DID + audit-log NFT.
