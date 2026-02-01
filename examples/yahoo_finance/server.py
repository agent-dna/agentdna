# server.py
import os
import sys
import json
import builtins
import asyncio
import concurrent.futures
from dotenv import load_dotenv

import yfinance as yf
from mcp.server.fastmcp import FastMCP
from agentdna import AgentDNA

# ─────────────────────────────
# Force stdout → stderr (MCP)
# ─────────────────────────────

_original_print = builtins.print
def _stderr_print(*args, **kwargs):
    _original_print(*args, file=sys.stderr)
builtins.print = _stderr_print

# ─────────────────────────────
# Environment
# ─────────────────────────────

load_dotenv()

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
MCP_TOOL_NAME = os.environ.get("MCP_TOOL_NAME")

if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")
if not MCP_TOOL_NAME:
    raise RuntimeError("Missing MCP_TOOL_NAME")

dna = AgentDNA(alias=MCP_TOOL_NAME, role="remote", api_key=AGENTDNA_API_KEY)

mcp = FastMCP("YahooFinanceMCP")

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# ─────────────────────────────
# AgentDNA helpers
# ─────────────────────────────

async def _verify_host_envelope(dna_envelope):
    if not dna_envelope:
        return None, None, None
    text = json.dumps(dna_envelope) if isinstance(dna_envelope, dict) else dna_envelope
    info = await dna.handle(raw_text=text, verify_mode="light")
    return info.get("original_message"), info.get("host_block"), info.get("trust_issues")

def _build_response(original_message, payload, host_block, trust_issues):
    built = dna.build(
        original_message=original_message or json.dumps(payload),
        response=json.dumps(payload),
        host_block=host_block,
        extra={"host_trust_issues": trust_issues},
    )
    return built["combined_json"]

# ─────────────────────────────
# Blocking Yahoo ops (isolated)
# ─────────────────────────────

def _quote_blocking(symbol: str):
    t = yf.Ticker(symbol)

    # First attempt: fast_info
    info = t.fast_info or {}
    price = info.get("last_price")

    # Fallback: recent close from history
    if price is None:
        hist = t.history(period="1d")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])

    return {
        "ok": price is not None,
        "symbol": symbol,
        "price": price,
        "currency": info.get("currency"),
        "exchange": info.get("exchange"),
    }

def _history_blocking(symbol: str, period: str):
    t = yf.Ticker(symbol)
    hist = t.history(period=period)
    rows = []
    for idx, row in hist.iterrows():
        rows.append({
            "date": idx.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": int(row["Volume"]),
        })
    return {"ok": True, "symbol": symbol, "rows": rows}

# ─────────────────────────────
# MCP tools (ASYNC + TIMEOUT)
# ─────────────────────────────

@mcp.tool()
async def get_quote(symbol: str, dna_envelope=None):
    orig, host_block, issues = await _verify_host_envelope(dna_envelope)
    loop = asyncio.get_running_loop()
    try:
        payload = await asyncio.wait_for(
            loop.run_in_executor(_executor, _quote_blocking, symbol),
            timeout=5.0,
        )
    except Exception as e:
        payload = {"ok": False, "error": str(e)}
    return _build_response(orig, payload, host_block, issues)

@mcp.tool()
async def get_history(symbol: str, period: str = "1mo", dna_envelope=None):
    orig, host_block, issues = await _verify_host_envelope(dna_envelope)
    loop = asyncio.get_running_loop()
    try:
        payload = await asyncio.wait_for(
            loop.run_in_executor(_executor, _history_blocking, symbol, period),
            timeout=5.0,
        )
    except Exception as e:
        payload = {"ok": False, "error": str(e)}
    return _build_response(orig, payload, host_block, issues)

if __name__ == "__main__":
    mcp.run(transport="stdio")
