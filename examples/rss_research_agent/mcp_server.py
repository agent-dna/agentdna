from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

import feedparser
from fastmcp import FastMCP

from config import settings

from agentdna import AgentDNA
from agentdna.integrations.mcp.fastmcp.middleware import AgentDNAMCPMiddleware

mcp_server_dna = AgentDNA(
    name="RSS Research MCP",
    type="tool",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
    admin_server_url=settings.admin_server_url,
)

mcp = FastMCP("rss-research-mcp")
mcp.add_middleware(AgentDNAMCPMiddleware(mcp_server_dna))

def _connection() -> sqlite3.Connection:
    settings.cache_database.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(settings.cache_database)


def initialize_cache() -> None:
    with _connection() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, feed_id TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT, summary TEXT NOT NULL)")


def _feed(feed_id: str) -> dict[str, str]:
    for feed in settings.feeds:
        if feed["id"] == feed_id:
            return feed
    raise ValueError("Unknown configured feed")


def _refresh(feed_id: str) -> None:
    feed = _feed(feed_id)
    parsed = feedparser.parse(feed["url"])
    if getattr(parsed, "bozo", False) and not getattr(parsed, "entries", []):
        raise RuntimeError("Configured RSS feed could not be parsed")
    with _connection() as connection:
        for entry in parsed.entries:
            url = str(entry.get("link", ""))
            if not url:
                continue
            article_id = hashlib.sha256(f"{feed_id}:{url}".encode()).hexdigest()[:24]
            connection.execute(
                "INSERT OR REPLACE INTO articles VALUES (?, ?, ?, ?, ?, ?)",
                (article_id, feed_id, str(entry.get("title", "Untitled")), url, str(entry.get("published", "")), str(entry.get("summary", ""))),
            )


def _limit(limit: int) -> int:
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    return limit


def _articles(rows: list[tuple[str, str, str, str, str, str]]) -> list[dict[str, str]]:
    return [{"id": row[0], "feed_id": row[1], "title": row[2], "url": row[3], "published_at": row[4], "summary": row[5]} for row in rows]


@mcp.tool()
def rss_list_feeds() -> dict[str, list[dict[str, str]]]:
    """List only the configured RSS feeds."""
    return {"feeds": [{"id": feed["id"], "name": feed["name"]} for feed in settings.feeds]}


@mcp.tool()
def rss_get_latest_articles(feed_id: str, limit: int = 10) -> dict[str, object]:
    """Refresh one configured feed and return a bounded list of articles."""
    initialize_cache()
    _refresh(feed_id)
    with _connection() as connection:
        rows = connection.execute("SELECT id, feed_id, title, url, published_at, summary FROM articles WHERE feed_id = ? ORDER BY rowid DESC LIMIT ?", (feed_id, _limit(limit))).fetchall()
    return {"articles": _articles(rows), "refreshed_at": datetime.now(timezone.utc).isoformat()}


@mcp.tool()
def rss_search_articles(query: str, limit: int = 10) -> dict[str, list[dict[str, str]]]:
    """Search cached configured-feed articles by keyword."""
    if not 1 <= len(query.strip()) <= 120:
        raise ValueError("query length must be between 1 and 120")
    initialize_cache()
    with _connection() as connection:
        rows = connection.execute("SELECT id, feed_id, title, url, published_at, summary FROM articles WHERE title LIKE ? OR summary LIKE ? ORDER BY rowid DESC LIMIT ?", (f"%{query}%", f"%{query}%", _limit(limit))).fetchall()
    return {"articles": _articles(rows)}


@mcp.tool()
def rss_get_article(article_id: str) -> dict[str, str]:
    """Return one cached article by server-generated identifier."""
    if len(article_id) != 24:
        raise ValueError("Invalid article id")
    initialize_cache()
    with _connection() as connection:
        row = connection.execute("SELECT id, feed_id, title, url, published_at, summary FROM articles WHERE id = ?", (article_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown article id")
    return _articles([row])[0]


if __name__ == "__main__":
    initialize_cache()
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port, path="/mcp")