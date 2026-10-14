"""The travel research tools, independent of MCP and AgentDNA.

Every tool uses public data that needs no API key:

    geocode            Open-Meteo geocoding        geocoding-api.open-meteo.com
    weather_forecast   Open-Meteo forecasts        api.open-meteo.com
    wikipedia_summary  Wikipedia REST API          en.wikipedia.org
    convert_currency   Frankfurter (ECB rates)     api.frankfurter.dev
    local_time         computed locally (IANA time zones)
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from config import settings

# Wikipedia asks API clients to identify themselves.
USER_AGENT = "AgentDNA-travel-swarm-example/1.0 (https://github.com/agent-dna/agentdna)"
MAX_FORECAST_DAYS = 7
CURRENCY = re.compile(r"^[A-Z]{3}$")


async def _get(url: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    ) as client:
        response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def geocode(city: str, count: int = 3) -> list[dict[str, Any]]:
    """Find places by name: coordinates, country, time zone and population."""

    if not city.strip():
        raise ValueError("city must not be empty")
    data = await _get(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": city.strip(), "count": max(1, min(count, 10)), "language": "en", "format": "json"},
    )
    return [
        {
            "name": place.get("name"),
            "country": place.get("country"),
            "region": place.get("admin1"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "timezone": place.get("timezone"),
            "population": place.get("population"),
        }
        for place in data.get("results") or []
    ]


async def weather_forecast(latitude: float, longitude: float, days: int = 3) -> dict[str, Any]:
    """Daily forecast for a location: min/max temperature (°C), rain probability (%)
    and WMO weather code, in the location's own time zone."""

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("latitude must be within ±90 and longitude within ±180")
    data = await _get(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": latitude,
            "longitude": longitude,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "auto",
            "forecast_days": max(1, min(days, MAX_FORECAST_DAYS)),
        },
    )
    daily = data.get("daily") or {}
    return {
        "timezone": data.get("timezone"),
        "days": [
            {
                "date": date,
                "min_c": daily["temperature_2m_min"][i],
                "max_c": daily["temperature_2m_max"][i],
                "rain_probability_pct": daily["precipitation_probability_max"][i],
                "weather_code": daily["weather_code"][i],
            }
            for i, date in enumerate(daily.get("time") or [])
        ],
    }


async def wikipedia_summary(title: str) -> dict[str, Any]:
    """The lead summary of an English Wikipedia article."""

    if not title.strip():
        raise ValueError("title must not be empty")
    data = await _get(
        "https://en.wikipedia.org/api/rest_v1/page/summary/"
        + quote(title.strip().replace(" ", "_"), safe="")
    )
    return {
        "title": data.get("title"),
        "description": data.get("description"),
        "summary": data.get("extract"),
        "url": (data.get("content_urls") or {}).get("desktop", {}).get("page"),
    }


async def convert_currency(amount: float, from_currency: str, to_currency: str) -> dict[str, Any]:
    """Convert an amount between currencies at the latest European Central Bank rate."""

    source, target = from_currency.strip().upper(), to_currency.strip().upper()
    if not (CURRENCY.match(source) and CURRENCY.match(target)):
        raise ValueError("currencies must be three-letter ISO 4217 codes, e.g. USD or EUR")
    if amount < 0:
        raise ValueError("amount must not be negative")
    data = await _get(
        "https://api.frankfurter.dev/v1/latest",
        {"amount": amount, "base": source, "symbols": target},
    )
    return {
        "amount": amount,
        "from": source,
        "to": target,
        "converted": (data.get("rates") or {}).get(target),
        "rate_date": data.get("date"),
    }


def local_time(timezone: str) -> dict[str, str]:
    """The current local date and time in an IANA time zone, e.g. Europe/Lisbon."""

    try:
        zone = ZoneInfo(timezone.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown IANA time zone {timezone!r}")
    now = datetime.now(zone)
    return {"timezone": timezone.strip(), "local_time": now.isoformat(timespec="minutes")}


TOOLS = [geocode, weather_forecast, wikipedia_summary, convert_currency, local_time]
