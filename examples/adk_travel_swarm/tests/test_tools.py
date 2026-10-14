"""The travel tools' input checks and the local-time tool: no network, no keys."""

import asyncio

import pytest
from travel_tools import convert_currency, geocode, local_time, weather_forecast, wikipedia_summary


def test_local_time_in_a_known_zone():
    result = local_time("Europe/Lisbon")
    assert result["timezone"] == "Europe/Lisbon"
    assert "T" in result["local_time"]


def test_local_time_rejects_an_unknown_zone():
    with pytest.raises(ValueError, match="unknown IANA time zone"):
        local_time("Mars/Olympus_Mons")


@pytest.mark.parametrize(
    "call",
    [
        lambda: geocode("  "),
        lambda: weather_forecast(91, 0),
        lambda: weather_forecast(0, 181),
        lambda: wikipedia_summary(""),
        lambda: convert_currency(100, "dollars", "EUR"),
        lambda: convert_currency(-5, "USD", "EUR"),
    ],
)
def test_invalid_input_is_refused_before_any_request(call):
    with pytest.raises(ValueError):
        asyncio.run(call())
