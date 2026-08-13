#!/usr/bin/env python3
"""Trello REST-API MCP server (FastMCP).

Exposes the one Trello endpoint the Release Announcer agent needs: create a card.
Start it, point the agent at it with TRELLO_MCP_URL, run `python run.py --live`.

    python mcp_server.py           # serves http://127.0.0.1:9008/mcp/

Auth: set TRELLO_KEY, TRELLO_TOKEN and TRELLO_LIST_ID (the Announcements list).
Without key/token the server runs in MOCK mode.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(Path(__file__).with_name(".env"))

HOST = os.getenv("MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("MCP_PORT", "9008"))

TRELLO_KEY = os.getenv("TRELLO_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_LIST_ID", "")
API = "https://api.trello.com/1"

mcp = FastMCP("Trello")

@mcp.tool
async def create_card(title: str, description: str, list_id: str = "") -> dict:
    """Create a card in the Announcements list (defaults to TRELLO_LIST_ID)."""
    target_list = list_id or TRELLO_LIST_ID
    if not (TRELLO_KEY and TRELLO_TOKEN):
        return {"_mock": True, "list": target_list, "title": title, "description": description}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{API}/cards",
            params={
                "key": TRELLO_KEY,
                "token": TRELLO_TOKEN,
                "idList": target_list,
                "name": title,
                "desc": description,
            },
        )
        r.raise_for_status()
        return {"status": r.status_code, "card_id": r.json().get("id"), "url": r.json().get("url")}


if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT)