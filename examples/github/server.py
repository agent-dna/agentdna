# server.py
import os
import sys
import builtins

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from agentdna import AgentDNA

# ─────────────────────────────
# Force print → stderr (MCP stdio)
# ─────────────────────────────

_original_print = builtins.print


def _stderr_print(*args, **kwargs):
    _original_print(
        *args,
        file=sys.stderr,
        **{k: v for k, v in kwargs.items() if k != "file"},
    )


builtins.print = _stderr_print

# ─────────────────────────────
# Env + GitHub + AgentDNA setup
# ─────────────────────────────

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
MCP_TOOL_NAME = os.environ.get("MCP_TOOL_NAME")

if not GITHUB_TOKEN:
    raise RuntimeError("Set GITHUB_TOKEN environment variable")
if not AGENTDNA_API_KEY:
    raise RuntimeError("Set AGENTDNA_API_KEY environment variable")
if not MCP_TOOL_NAME:
    raise RuntimeError("Set MCP_TOOL_NAME environment variable")

# 🔒 FIXED REPO CONFIG
REPO_OWNER = "SynapzeCore"
REPO_NAME = "sample-repo"
BASE_BRANCH = "main"

REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
API_BASE = "https://api.github.com"

mcp = FastMCP("GitHubMCP")

# Pure-remote agent — never writes to chain, so enable_nft=False skips deploy.
dna = AgentDNA(alias=MCP_TOOL_NAME, api_key=AGENTDNA_API_KEY, enable_nft=False)
print("[SERVER] ✅ GitHub MCP server DID:", dna.trust.did)
print("[SERVER] ✅ Repo URL:", REPO_URL)


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


# ─────────────────────────────
# MCP TOOLS
# ─────────────────────────────
# Each tool follows the same three-step pattern:
#   1. ctx = await dna.verify_request(dna_envelope)
#   2. business logic
#   3. return dna.sign_response(payload, ctx=ctx)


@mcp.tool()
async def create_issue(
    title: str,
    description: str,
    dna_envelope: dict | str | None = None,
) -> str:
    print("[SERVER] create_issue called")
    ctx = await dna.verify_request(dna_envelope)
    if not ctx.verified:
        return dna.sign_response(
            {"ok": False, "error": "failed to verify signature of Agent"},
            ctx=ctx,
        )

    resp = requests.post(
        f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/issues",
        headers=_gh_headers(),
        json={"title": title, "body": description},
    )

    if resp.status_code == 201:
        payload = {"ok": True, "issue_url": resp.json().get("html_url")}
    else:
        payload = {"ok": False, "status_code": resp.status_code, "error": resp.text}

    return dna.sign_response(payload, ctx=ctx)


@mcp.tool()
async def create_pull_request(
    title: str,
    description: str,
    head: str,
    dna_envelope: dict | str | None = None,
) -> str:
    print("[SERVER] create_pull_request called")
    ctx = await dna.verify_request(dna_envelope)
    if not ctx.verified:
        return dna.sign_response(
            {"ok": False, "error": "failed to verify signature of Agent"},
            ctx=ctx,
        )

    resp = requests.post(
        f"{API_BASE}/repos/{REPO_OWNER}/{REPO_NAME}/pulls",
        headers=_gh_headers(),
        json={
            "title": title,
            "body": description,
            "head": head,
            "base": BASE_BRANCH,
        },
    )

    if resp.status_code == 201:
        payload = {"ok": True, "pr_url": resp.json().get("html_url")}
    else:
        payload = {"ok": False, "status_code": resp.status_code, "error": resp.text}

    return dna.sign_response(payload, ctx=ctx)


if __name__ == "__main__":
    mcp.run(transport="stdio")
