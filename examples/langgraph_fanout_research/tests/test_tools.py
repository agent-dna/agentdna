"""The research tools' input checks: no network, no keys."""

import asyncio

import pytest
from research_tools import (
    INDICATORS,
    country_indicator,
    find_country,
    wikipedia_search,
    wikipedia_summary,
)


@pytest.mark.parametrize(
    "call",
    [
        lambda: find_country("  "),
        lambda: country_indicator("Germany", "population"),
        lambda: country_indicator("DE", "population"),
        lambda: country_indicator("DEU", "happiness"),
        lambda: country_indicator("DEU", "population", 2024, 2015),
        lambda: country_indicator("DEU", "population", 1900, 2015),
        lambda: wikipedia_search(""),
        lambda: wikipedia_summary(" "),
    ],
)
def test_invalid_input_is_refused_before_any_request(call):
    with pytest.raises(ValueError):
        asyncio.run(call())


def test_indicators_are_world_bank_codes():
    assert set(INDICATORS) == {
        "population",
        "gdp_per_capita_usd",
        "renewable_energy_share_pct",
        "electricity_access_pct",
        "internet_users_pct",
    }
    assert all(code.count(".") >= 2 for code in INDICATORS.values())
