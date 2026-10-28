"""The research tools, independent of MCP.

Every tool uses public data that needs no API key:

    find_country        World Bank country metadata   api.worldbank.org
    country_indicator   World Bank indicators         api.worldbank.org
    wikipedia_search    Wikipedia search API          en.wikipedia.org
    wikipedia_summary   Wikipedia REST API            en.wikipedia.org
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import httpx
from config import settings

# Wikimedia's robot policy asks API clients to identify themselves with a way
# to reach them, and blocks clients that don't.
USER_AGENT = f"langgraph-fanout-research-example/1.0 ({settings.contact})"

# Wikipedia rate-limits bursts; every worker shares this many concurrent requests.
_wikipedia = asyncio.Semaphore(settings.wikipedia_concurrency)

# The indicators the agents may ask for, by a readable name.
INDICATORS = {
    "population": "SP.POP.TOTL",
    "gdp_per_capita_usd": "NY.GDP.PCAP.CD",
    "renewable_energy_share_pct": "EG.FEC.RNEW.ZS",
    "electricity_access_pct": "EG.ELC.ACCS.ZS",
    "internet_users_pct": "IT.NET.USER.ZS",
}

_countries: list[dict[str, Any]] | None = None


def _retry_after(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After", "")
    return min(float(value), 30) if value.isdigit() else 2**attempt


async def _get(url: str, params: dict[str, Any] | None = None, retries: int = 3) -> Any:
    """GET JSON, backing off when the server says it is overloaded or rate-limiting."""

    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    ) as client:
        for attempt in range(retries + 1):
            response = await client.get(url, params=params)
            if response.status_code not in (429, 503) or attempt == retries:
                break
            await asyncio.sleep(_retry_after(response, attempt))
    response.raise_for_status()
    return response.json()


async def _wikipedia_get(url: str, params: dict[str, Any] | None = None) -> Any:
    async with _wikipedia:
        return await _get(url, params)


async def _all_countries() -> list[dict[str, Any]]:
    """World Bank countries, without regional aggregates. Fetched once."""

    global _countries
    if _countries is None:
        _, rows = await _get(
            "https://api.worldbank.org/v2/country", {"format": "json", "per_page": 400}
        )
        _countries = [row for row in rows if row["region"]["value"].strip() != "Aggregates"]
    return _countries


async def find_country(name: str) -> list[dict[str, Any]]:
    """Find countries by name: ISO3 code, region, income level and capital city."""

    needle = name.strip().lower()
    if not needle:
        raise ValueError("name must not be empty")
    return [
        {
            "code": row["id"],
            "name": row["name"],
            "region": row["region"]["value"].strip(),
            "income_level": row["incomeLevel"]["value"].strip(),
            "capital": row["capitalCity"],
        }
        for row in await _all_countries()
        if needle in row["name"].lower()
    ][:5]


async def country_indicator(
    country_code: str, indicator: str, start_year: int = 2015, end_year: int = 2024
) -> dict[str, Any]:
    """A World Bank indicator for one country over a range of years.

    `country_code` is an ISO3 code from find_country. `indicator` is one of:
    population, gdp_per_capita_usd, renewable_energy_share_pct,
    electricity_access_pct, internet_users_pct.
    """

    code = country_code.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("country_code must be an ISO3 code, e.g. DEU")
    if indicator not in INDICATORS:
        raise ValueError(f"indicator must be one of {sorted(INDICATORS)}")
    if not 1960 <= start_year <= end_year <= 2100:
        raise ValueError("years must satisfy 1960 <= start_year <= end_year")

    _, rows = await _get(
        f"https://api.worldbank.org/v2/country/{code}/indicator/{INDICATORS[indicator]}",
        {"format": "json", "date": f"{start_year}:{end_year}", "per_page": 100},
    )
    rows = rows or []
    return {
        "country": rows[0]["country"]["value"] if rows else code,
        "indicator": indicator,
        "description": rows[0]["indicator"]["value"] if rows else "",
        "values": [
            {"year": int(row["date"]), "value": row["value"]}
            for row in sorted(rows, key=lambda r: r["date"])
            if row["value"] is not None
        ],
    }


async def wikipedia_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Search English Wikipedia; returns article titles to read with wikipedia_summary."""

    if not query.strip():
        raise ValueError("query must not be empty")
    data = await _wikipedia_get(
        "https://en.wikipedia.org/w/api.php",
        {
            "action": "query",
            "list": "search",
            "srsearch": query.strip(),
            "srlimit": max(1, min(limit, 10)),
            "format": "json",
        },
    )
    return [{"title": hit["title"]} for hit in data.get("query", {}).get("search", [])]


async def wikipedia_summary(title: str) -> dict[str, Any]:
    """The lead summary of an English Wikipedia article."""

    if not title.strip():
        raise ValueError("title must not be empty")
    data = await _wikipedia_get(
        "https://en.wikipedia.org/api/rest_v1/page/summary/"
        + quote(title.strip().replace(" ", "_"), safe="")
    )
    return {
        "title": data.get("title"),
        "description": data.get("description"),
        "summary": data.get("extract"),
        "url": (data.get("content_urls") or {}).get("desktop", {}).get("page"),
    }


TOOLS = [find_country, country_indicator, wikipedia_search, wikipedia_summary]
