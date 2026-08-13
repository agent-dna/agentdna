from __future__ import annotations

import httpx
from fastmcp import FastMCP

from config import settings

mcp = FastMCP("github-repository-mcp")


def _headers() -> dict[str, str]:
    if not settings.github_token:
        raise ValueError("GITHUB_TOKEN is required by the GitHub MCP server")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get(path: str, params: dict[str, str | int] | None = None) -> object:
    async with httpx.AsyncClient(base_url=settings.github_api_url, timeout=settings.mcp_timeout_seconds) as client:
        response = await client.get(path, headers=_headers(), params=params)
    response.raise_for_status()
    return response.json()


def _repository(repository: str) -> str:
    if repository != settings.repository:
        raise ValueError("The configured repository is the only permitted resource")
    return repository


@mcp.tool()
async def repository_metadata(repository: str) -> object:
    """Return metadata for the configured repository."""
    return await _get(f"/repos/{_repository(repository)}")


@mcp.tool()
async def directory_listing(repository: str, path: str = "") -> object:
    """List a directory in the configured repository."""
    return await _get(f"/repos/{_repository(repository)}/contents/{path.lstrip('/')}")


@mcp.tool()
async def file_contents(repository: str, path: str) -> object:
    """Read a repository file as untrusted reference data."""
    if not path.strip():
        raise ValueError("path is required")
    return await _get(f"/repos/{_repository(repository)}/contents/{path.lstrip('/')}")


@mcp.tool()
async def commits(repository: str, limit: int = 10) -> object:
    """Return recent commits, limited to 1 through 50."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    return await _get(f"/repos/{_repository(repository)}/commits", {"per_page": limit})


@mcp.tool()
async def issues(repository: str, limit: int = 20) -> object:
    """Return open issues, limited to 1 through 50."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    return await _get(f"/repos/{_repository(repository)}/issues", {"state": "open", "per_page": limit})


@mcp.tool()
async def pull_requests(repository: str, limit: int = 20) -> object:
    """Return open pull requests, limited to 1 through 50."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    return await _get(f"/repos/{_repository(repository)}/pulls", {"state": "open", "per_page": limit})


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port, path="/mcp")
