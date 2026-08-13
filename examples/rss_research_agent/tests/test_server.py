import json
from dataclasses import replace

import mcp_server as mcp_server
from mcp_server import _limit, rss_get_article, rss_list_feeds


def test_only_configured_feeds_are_listed(tmp_path, monkeypatch) -> None:
    feeds = [
        {"id": "cisa", "name": "CISA Alerts", "url": "https://www.cisa.gov/cybersecurity-advisories/all.xml"},
        {"id": "github", "name": "GitHub Blog", "url": "https://github.blog/feed/"},
    ]
    monkeypatch.setattr(
        mcp_server,
        "settings",
        replace(mcp_server.settings, cache_database=tmp_path / "rss.db", feeds_json=json.dumps(feeds)),
    )
    assert {feed["id"] for feed in rss_list_feeds()["feeds"]} == {"cisa", "github"}


def test_invalid_article_id_and_limit_are_rejected() -> None:
    try:
        rss_get_article("invalid")
    except ValueError as error:
        assert "Invalid article id" in str(error)
    else:
        raise AssertionError("invalid article id was not rejected")
    try:
        _limit(21)
    except ValueError as error:
        assert "between 1 and 20" in str(error)
    else:
        raise AssertionError("excessive limit was not rejected")