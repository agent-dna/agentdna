"""The package tools' input checks and response parsing: no network."""

import asyncio

import package_tools
import pytest
from package_tools import (
    dependencies,
    download_stats,
    find_vulnerabilities,
    package_info,
    project_health,
    release_history,
    vulnerability_details,
)


@pytest.mark.parametrize(
    "call",
    [
        lambda: package_info(""),
        lambda: package_info("../etc/passwd"),
        lambda: release_history("-leading-dash"),
        lambda: download_stats("has space"),
        lambda: find_vulnerabilities("requests", ""),
        lambda: find_vulnerabilities("requests", "1.0/../2"),
        lambda: vulnerability_details("GHSA 1"),
        lambda: dependencies("requests", " "),
    ],
)
def test_invalid_input_is_refused_before_any_request(call, monkeypatch):
    async def no_network(*args, **kwargs):
        raise AssertionError("no request may be made for invalid input")

    monkeypatch.setattr(package_tools, "_request", no_network)
    with pytest.raises(ValueError):
        asyncio.run(call())


def answer(responses):
    """A stand-in for the HTTP layer: the JSON for each URL, in order of the calls."""

    calls = []

    async def request(method, url, json=None):
        calls.append((method, url, json))
        for prefix, body in responses.items():
            if url.startswith(prefix):
                return body
        raise AssertionError(f"unexpected request {method} {url}")

    return request, calls


PYPI = {
    "info": {
        "name": "demo",
        "summary": "A demo package.",
        "version": "2.0.0",
        "license_expression": "MIT",
        "requires_python": ">=3.10",
        "author": "Ada",
        "project_urls": {"Source": "https://github.com/acme/demo"},
    },
    "releases": {
        "1.0.0": [{"upload_time_iso_8601": "2023-01-01T00:00:00Z", "yanked": False}],
        "1.5.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00Z", "yanked": True}],
        "2.0.0": [{"upload_time_iso_8601": "2025-01-01T00:00:00Z", "yanked": False}],
        "0.0.1": [],
    },
}


def test_package_info_summarises_pypi_metadata(monkeypatch):
    request, _ = answer({"https://pypi.org/pypi/demo/json": PYPI})
    monkeypatch.setattr(package_tools, "_request", request)

    info = asyncio.run(package_info("demo"))
    assert info["latest_version"] == "2.0.0"
    assert info["license"] == "MIT"
    assert info["release_count"] == 4
    assert (info["first_release"], info["latest_release"]) == (
        "2023-01-01T00:00:00Z",
        "2025-01-01T00:00:00Z",
    )


def test_release_history_is_newest_first_and_flags_yanked(monkeypatch):
    request, _ = answer({"https://pypi.org/pypi/demo/json": PYPI})
    monkeypatch.setattr(package_tools, "_request", request)

    history = asyncio.run(release_history("demo", limit=2))
    assert [(r["version"], r["yanked"]) for r in history] == [("2.0.0", False), ("1.5.0", True)]


def test_find_vulnerabilities_reports_fixes_for_this_package(monkeypatch):
    osv = {
        "vulns": [
            {
                "id": "GHSA-1",
                "aliases": ["CVE-1"],
                "summary": "Bad thing",
                "database_specific": {"severity": "HIGH"},
                "affected": [
                    {
                        "package": {"name": "demo", "ecosystem": "PyPI"},
                        "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.2.0"}]}],
                    },
                    {
                        "package": {"name": "other", "ecosystem": "PyPI"},
                        "ranges": [{"events": [{"fixed": "9.9.9"}]}],
                    },
                ],
            }
        ]
    }
    request, calls = answer({"https://api.osv.dev/v1/query": osv})
    monkeypatch.setattr(package_tools, "_request", request)

    [vuln] = asyncio.run(find_vulnerabilities("demo", "1.0.0"))
    assert vuln == {
        "id": "GHSA-1",
        "aliases": ["CVE-1"],
        "summary": "Bad thing",
        "severity": "HIGH",
        "fixed_in": ["1.2.0"],
    }
    assert calls[0][2] == {"package": {"name": "demo", "ecosystem": "PyPI"}, "version": "1.0.0"}


def test_dependencies_split_direct_and_indirect(monkeypatch):
    graph = {
        "nodes": [
            {"versionKey": {"name": "demo", "version": "2.0.0"}, "relation": "SELF"},
            {"versionKey": {"name": "a", "version": "1.0"}, "relation": "DIRECT"},
            {"versionKey": {"name": "b", "version": "2.0"}, "relation": "INDIRECT"},
        ],
        "edges": [
            {"fromNode": 0, "toNode": 1, "requirement": ">=1"},
            {"fromNode": 1, "toNode": 2, "requirement": "<3"},
        ],
        "error": "",
    }
    request, _ = answer(
        {"https://api.deps.dev/v3/systems/pypi/packages/demo/versions/2.0.0": graph}
    )
    monkeypatch.setattr(package_tools, "_request", request)

    result = asyncio.run(dependencies("demo", "2.0.0"))
    assert result["direct"] == [{"name": "a", "version": "1.0", "requirement": ">=1"}]
    assert result["indirect"] == ["b"]
    assert (result["total"], result["error"]) == (2, None)


PROJECT = {
    "starsCount": 10,
    "forksCount": 2,
    "openIssuesCount": 3,
    "license": "MIT",
    "scorecard": {
        "overallScore": 7.5,
        "date": "2026-01-01",
        "checks": [{"name": "Maintained", "score": 10}],
    },
}


def test_project_health_finds_the_repository_on_pypi(monkeypatch):
    pypi = {
        "info": {
            "project_urls": {
                "Funding": "https://github.com/sponsors/ada",
                "Documentation": "https://demo.readthedocs.io",
                "Changelog": "https://github.com/acme/old-name/blob/main/CHANGELOG.md",
                "Source Code": "https://github.com/acme/demo",
            }
        }
    }
    request, calls = answer(
        {
            "https://pypi.org/pypi/demo/json": pypi,
            "https://api.deps.dev/v3/projects/github.com%2Facme%2Fdemo": PROJECT,
        }
    )
    monkeypatch.setattr(package_tools, "_request", request)

    health = asyncio.run(project_health("demo"))
    assert health["source_repo"] == "https://github.com/acme/demo"
    assert (health["stars"], health["open_issues"]) == (10, 3)
    assert health["scorecard"] == {
        "overall": 7.5,
        "date": "2026-01-01",
        "checks": {"Maintained": 10},
    }
    assert len(calls) == 2  # deps.dev's package links are not needed


def test_project_health_falls_back_to_deps_dev_links(monkeypatch):
    base = "https://api.deps.dev/v3/systems/pypi/packages/demo"
    request, _ = answer(
        {
            "https://pypi.org/pypi/demo/json": {"info": {"project_urls": None, "home_page": ""}},
            f"{base}/versions/2.0.0": {
                "links": [{"label": "SOURCE_REPO", "url": "https://github.com/acme/demo.git"}]
            },
            base: {
                "versions": [
                    {"versionKey": {"version": "1.0.0"}, "isDefault": False},
                    {"versionKey": {"version": "2.0.0"}, "isDefault": True},
                ]
            },
            "https://api.deps.dev/v3/projects/github.com%2Facme%2Fdemo": PROJECT,
        }
    )
    monkeypatch.setattr(package_tools, "_request", request)

    assert asyncio.run(project_health("demo"))["source_repo"] == "https://github.com/acme/demo"


def test_project_health_without_a_known_repository(monkeypatch):
    request, _ = answer(
        {
            "https://pypi.org/pypi/demo/json": {"info": {"project_urls": {"Docs": "https://x.io"}}},
            "https://api.deps.dev/v3/systems/pypi/packages/demo": {"versions": []},
        }
    )
    monkeypatch.setattr(package_tools, "_request", request)

    assert asyncio.run(project_health("demo"))["source_repo"] is None
