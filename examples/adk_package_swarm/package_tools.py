"""Package-intelligence tools, independent of MCP and ADK.

Every tool uses public data that needs no API key:

    package_info, release_history   PyPI JSON API          pypi.org
    download_stats                  PyPI download counts   pypistats.org
    find_vulnerabilities,           Open Source            api.osv.dev
    vulnerability_details           Vulnerabilities
    dependencies, project_health    Open Source Insights   api.deps.dev
                                    (with OpenSSF Scorecard)
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from config import settings

USER_AGENT = "adk-package-swarm-example/1.0"

# PEP 508 project names.
_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
_VULN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
# Hosts deps.dev knows source repositories on.
_REPO_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
# Paths on those hosts that are accounts' pages, not repositories.
_NOT_REPOS = {"sponsors", "orgs", "users", "features", "marketplace"}


def _check_name(name: str) -> str:
    name = name.strip()
    if not _NAME.match(name):
        raise ValueError(f"not a valid PyPI package name: {name!r}")
    return name


def _check_version(version: str) -> str:
    version = version.strip()
    if not _VERSION.match(version):
        raise ValueError(f"not a valid package version: {version!r}")
    return version


async def _request(method: str, url: str, json: Any = None) -> Any:
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, headers={"User-Agent": USER_AGENT}
    ) as client:
        response = await client.request(method, url, json=json)
    if response.status_code == 404:
        raise LookupError(f"not found: {url}")
    response.raise_for_status()
    return response.json()


async def _get(url: str) -> Any:
    return await _request("GET", url)


async def _post(url: str, json: Any) -> Any:
    return await _request("POST", url, json=json)


# -- PyPI ------------------------------------------------------------------------


def _upload_time(files: list[dict[str, Any]]) -> str | None:
    times = [f["upload_time_iso_8601"] for f in files if f.get("upload_time_iso_8601")]
    return min(times) if times else None


async def package_info(name: str) -> dict[str, Any]:
    """A PyPI package's metadata: latest version, licence, Python support, links, release span."""

    data = await _get(f"https://pypi.org/pypi/{quote(_check_name(name))}/json")
    info = data["info"]
    released = sorted(
        t for t in (_upload_time(files) for files in data.get("releases", {}).values()) if t
    )
    return {
        "name": info["name"],
        "summary": info.get("summary"),
        "latest_version": info["version"],
        "license": info.get("license_expression") or (info.get("license") or "")[:200] or None,
        "requires_python": info.get("requires_python"),
        "author": info.get("author") or info.get("author_email"),
        "project_urls": info.get("project_urls") or {},
        "release_count": len(data.get("releases", {})),
        "first_release": released[0] if released else None,
        "latest_release": released[-1] if released else None,
    }


async def release_history(name: str, limit: int = 10) -> list[dict[str, Any]]:
    """A PyPI package's most recent releases, newest first, with upload dates and yanked flags."""

    data = await _get(f"https://pypi.org/pypi/{quote(_check_name(name))}/json")
    releases = [
        {
            "version": version,
            "uploaded": _upload_time(files),
            "yanked": any(f.get("yanked") for f in files),
        }
        for version, files in data.get("releases", {}).items()
        if files
    ]
    releases.sort(key=lambda r: r["uploaded"] or "", reverse=True)
    return releases[: max(1, min(limit, 50))]


async def download_stats(name: str) -> dict[str, Any]:
    """Recent PyPI downloads of a package: last day, week and month."""

    data = await _get(
        f"https://pypistats.org/api/packages/{quote(_check_name(name).lower())}/recent"
    )
    return {"package": data["package"], **data["data"]}


# -- OSV -------------------------------------------------------------------------


def _fixed_versions(vuln: dict[str, Any], name: str | None = None) -> list[str]:
    fixed = []
    for affected in vuln.get("affected", []):
        package = affected.get("package", {})
        if name and package.get("name", "").lower() != name.lower():
            continue
        for range_ in affected.get("ranges", []):
            fixed += [e["fixed"] for e in range_.get("events", []) if "fixed" in e]
    return sorted(set(fixed))


def _severity(vuln: dict[str, Any]) -> str | None:
    rated = (vuln.get("database_specific") or {}).get("severity")
    if rated:
        return rated
    scores = [s.get("score") for s in vuln.get("severity", []) if s.get("score")]
    return scores[0] if scores else None


async def find_vulnerabilities(name: str, version: str) -> list[dict[str, Any]]:
    """Known vulnerabilities affecting one version of a PyPI package, with the versions that fix them."""

    name, version = _check_name(name), _check_version(version)
    data = await _post(
        "https://api.osv.dev/v1/query",
        {"package": {"name": name, "ecosystem": "PyPI"}, "version": version},
    )
    return [
        {
            "id": vuln["id"],
            "aliases": vuln.get("aliases", []),
            "summary": vuln.get("summary"),
            "severity": _severity(vuln),
            "fixed_in": _fixed_versions(vuln, name),
        }
        for vuln in data.get("vulns", [])
    ]


async def vulnerability_details(vuln_id: str) -> dict[str, Any]:
    """One vulnerability's details from OSV: description, dates, severity, fixes and references."""

    vuln_id = vuln_id.strip()
    if not _VULN_ID.match(vuln_id):
        raise ValueError(f"not a valid vulnerability id: {vuln_id!r}")
    vuln = await _get(f"https://api.osv.dev/v1/vulns/{quote(vuln_id)}")
    return {
        "id": vuln["id"],
        "aliases": vuln.get("aliases", []),
        "summary": vuln.get("summary"),
        "details": (vuln.get("details") or "")[:1500],
        "severity": _severity(vuln),
        "published": vuln.get("published"),
        "modified": vuln.get("modified"),
        "fixed_in": _fixed_versions(vuln),
        "references": [r["url"] for r in vuln.get("references", [])][:5],
    }


# -- deps.dev ----------------------------------------------------------------------

_DEPS_DEV = "https://api.deps.dev/v3/systems/pypi/packages"


async def dependencies(name: str, version: str) -> dict[str, Any]:
    """The resolved dependency tree of one version of a PyPI package: direct and indirect dependencies."""

    name, version = _check_name(name), _check_version(version)
    data = await _get(
        f"{_DEPS_DEV}/{quote(name, safe='')}/versions/{quote(version, safe='')}:dependencies"
    )
    nodes = data.get("nodes", [])
    requirements = {
        e["toNode"]: e.get("requirement") for e in data.get("edges", []) if e["fromNode"] == 0
    }

    def describe(index: int) -> dict[str, Any]:
        key = nodes[index]["versionKey"]
        return {
            "name": key["name"],
            "version": key["version"],
            "requirement": requirements.get(index),
        }

    direct = [describe(i) for i, n in enumerate(nodes) if n.get("relation") == "DIRECT"]
    indirect = [describe(i)["name"] for i, n in enumerate(nodes) if n.get("relation") == "INDIRECT"]
    return {
        "package": name,
        "version": version,
        "direct": direct,
        "indirect": indirect,
        "total": len(direct) + len(indirect),
        "error": data.get("error") or None,
    }


def _project_key(url: str) -> str | None:
    """`github.com/owner/repo` from a source repository URL, or None."""

    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc.lower() not in _REPO_HOSTS or len(parts) < 2 or parts[0] in _NOT_REPOS:
        return None
    repo = parts[1][: -len(".git")] if parts[1].endswith(".git") else parts[1]
    return f"{parsed.netloc.lower()}/{parts[0]}/{repo}"


async def _source_repo(name: str) -> str | None:
    """The package's source repository, as a deps.dev project key.

    PyPI's project URLs come first: they describe the latest release. deps.dev's
    links are the fallback, but its "default" version can be a pre-release with
    no repository link.
    """

    info = (await _get(f"https://pypi.org/pypi/{quote(name)}/json"))["info"]
    labelled = (info.get("project_urls") or {}).items()
    # Links labelled as the source come first; funding links are never the repository.
    ranked = sorted(
        (item for item in labelled if not any(w in item[0].lower() for w in ("fund", "sponsor"))),
        key=lambda item: not any(w in item[0].lower() for w in ("source", "repo", "code")),
    )
    urls = [url for _, url in ranked] + [info.get("home_page") or ""]
    key = next((k for k in map(_project_key, urls) if k), None)
    if key is not None:
        return key

    package = await _get(f"{_DEPS_DEV}/{quote(name, safe='')}")
    default = next((v for v in package.get("versions", []) if v.get("isDefault")), None)
    if default is None:
        return None
    version = await _get(
        f"{_DEPS_DEV}/{quote(name, safe='')}/versions/{quote(default['versionKey']['version'], safe='')}"
    )
    repos = [link["url"] for link in version.get("links", []) if link.get("label") == "SOURCE_REPO"]
    return next((k for k in map(_project_key, repos) if k), None)


async def project_health(name: str) -> dict[str, Any]:
    """Health of a PyPI package's source repository: stars, open issues, and its OpenSSF Scorecard."""

    name = _check_name(name)
    key = await _source_repo(name)
    if key is None:
        return {"package": name, "source_repo": None, "note": "no known source repository"}

    try:
        project = await _get(f"https://api.deps.dev/v3/projects/{quote(key, safe='')}")
    except LookupError:
        return {
            "package": name,
            "source_repo": f"https://{key}",
            "note": "deps.dev has no data on it",
        }
    scorecard = project.get("scorecard") or {}
    return {
        "package": name,
        "source_repo": f"https://{key}",
        "stars": project.get("starsCount"),
        "forks": project.get("forksCount"),
        "open_issues": project.get("openIssuesCount"),
        "license": project.get("license"),
        "scorecard": {
            "overall": scorecard.get("overallScore"),
            "date": scorecard.get("date"),
            "checks": {c["name"]: c.get("score") for c in scorecard.get("checks", [])},
        }
        if scorecard
        else None,
    }


TOOLS = [
    package_info,
    release_history,
    download_stats,
    find_vulnerabilities,
    vulnerability_details,
    dependencies,
    project_health,
]
