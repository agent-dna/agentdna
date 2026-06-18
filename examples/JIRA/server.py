import os
import json
import requests
import sys
import builtins
from typing import Any, Dict, Tuple, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from agentdna import AgentDNA

from typing import Optional, Dict, Any

def verify_host_envelope(dna_envelope: Optional[Dict[str, Any]]) -> None:
    return

def _to_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                    }
                ],
            }
        ],
    }

# ─────────────────────────────
# Force print → stderr (MCP over stdio requirement)
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
# Env + Jira + AgentDNA setup
# ─────────────────────────────

load_dotenv()

mcp = FastMCP("JiraMCP")

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN")
AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")

if not (JIRA_BASE_URL and JIRA_EMAIL and JIRA_API_TOKEN):
    raise RuntimeError("Set JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN environment variables.")

if not AGENTDNA_API_KEY:
    raise RuntimeError("Set AGENTDNA_API_KEY environment variable.")


def _jira_auth() -> tuple[str, str]:
    return JIRA_EMAIL, JIRA_API_TOKEN


# ─────────────────────────────
# AgentDNA: Jira MCP server (remote role)
# ─────────────────────────────

dna = AgentDNA(alias="jira_server", api_key=AGENTDNA_API_KEY, kind="agent", enable_nft=False)
print("[SERVER] ✅ Jira MCP server DID:", dna.trust.did)
print("[SERVER] ✅ Jira MCP server base URL:", dna.trust.base_url)


async def _verify_host_envelope(
    dna_envelope: Optional[Any],
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[list]]:
    """
    Verify the host envelope (if present) and return:
      (original_message, host_block, trust_issues)

    dna_envelope may arrive as a JSON string or already-parsed dict.
    We normalize it to a JSON string for AgentDNA.handle().
    """
    print("[SERVER] verify_host_envelope: raw dna_envelope:", dna_envelope,
          "TYPE:", type(dna_envelope))

    if not dna_envelope:
        return None, None, None

    if isinstance(dna_envelope, dict):
        dna_envelope_str = json.dumps(dna_envelope)
        print("[SERVER] verify_host_envelope: converted dict → JSON string")
    elif isinstance(dna_envelope, str):
        dna_envelope_str = dna_envelope
    else:
        print("[SERVER] verify_host_envelope: unsupported type for dna_envelope")
        return None, None, ["Unsupported dna_envelope type"]

    info = await dna.handle(
        raw_text=dna_envelope_str,
        verify_mode="light",
    )

    print("[SERVER] verify_host_envelope: info:", info)

    host_block = info.get("host_block")
    trust_issues = info.get("trust_issues")
    # Support new envelope format (payload.message) with legacy fallback.
    original_message = (
        info.get("original_message")
        or (host_block or {}).get("envelope", {}).get("payload", {}).get("message")
    )

    return original_message, host_block, trust_issues


def _build_signed_response(
    original_message: Optional[str],
    jira_payload: str,
    host_block: Optional[Dict[str, Any]],
    trust_issues: Optional[list],
    inject_fake: bool = False,
) -> str:
    if original_message is None:
        original_message = jira_payload

    if inject_fake:
        print("[SERVER] Simulating tampering: changing original_message before signing")
        original_message = (original_message or "") + " [SERVER_TAMPERED]"

    built = dna.build(
        original_message=original_message,
        response=jira_payload,
        host_block=host_block,
        extra={"host_trust_issues": trust_issues},
    )

    print("[SERVER] dna.build returned:", built)
    print("[SERVER] dna.build keys:", getattr(built, "keys", lambda: [])())
 
    if isinstance(built, dict) and "combined_json" in built:
        return built["combined_json"]

    # Fallbacks (shouldn't normally hit these)
    if isinstance(built, str):
        return built
    return json.dumps(built)

# ─────────────────────────────
# MCP tools (DNA-aware)
# ─────────────────────────────

@mcp.tool()
async def search_issues(
    jql: str,
    max_results: int = 10,
    dna_envelope: dict | str | None = None,
    inject_fake: bool = False, 
) -> str:
    print("\n[SERVER] === search_issues CALLED ===")
    print("[SERVER] search_issues args → jql:", jql, "TYPE:", type(jql))
    print("[SERVER] search_issues args → max_results:", max_results, "TYPE:", type(max_results))
    print("[SERVER] search_issues args → dna_envelope TYPE:", type(dna_envelope))

    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    if original_message is None:
        original_message = json.dumps(
            {"tool": "search_issues", "jql": jql, "max_results": max_results}
        )

    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    payload = {
        "jql": jql,
        "maxResults": max_results,
    }

    print("[SERVER] search_issues: Jira URL:", url)
    print("[SERVER] search_issues: Jira payload:", payload)

    resp = requests.post(url, auth=_jira_auth(), json=payload)
    print("[SERVER] search_issues: Jira status code:", resp.status_code)
    resp.raise_for_status()
    data = resp.json()

    issues = []
    for issue in data.get("issues", []):
        fields = issue.get("fields", {})
        issues.append(
            {
                "key": issue.get("key"),
                "summary": fields.get("summary"),
                "status": fields.get("status", {}).get("name"),
                "assignee": (fields.get("assignee") or {}).get("displayName"),
            }
        )

    jira_payload = json.dumps(issues, indent=2)
    print("[SERVER] search_issues: returning", len(issues), "issues")
    return _build_signed_response(original_message, jira_payload, host_block, trust_issues, inject_fake=inject_fake)


@mcp.tool()
async def get_issue(
    key: str,
    dna_envelope: dict | str | None = None,
    inject_fake: bool = False, 
) -> str:
    print("\n[SERVER] === get_issue CALLED ===")
    print("[SERVER] get_issue args → key:", key, "TYPE:", type(key))

    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    if original_message is None:
        original_message = json.dumps({"tool": "get_issue", "key": key})

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{key}"
    print("[SERVER] get_issue: Jira URL:", url)

    resp = requests.get(url, auth=_jira_auth())
    print("[SERVER] get_issue: Jira status code:", resp.status_code)
    resp.raise_for_status()
    jira_payload = json.dumps(resp.json(), indent=2)

    return _build_signed_response(original_message, jira_payload, host_block, trust_issues, inject_fake=inject_fake)


@mcp.tool()
async def create_issue(
    project_key: str,
    summary: str,
    description: str,
    issue_type: str = "Task",
    dna_envelope: dict | str | None = None,
    inject_fake: bool = False, 
) -> str:
    print("\n[SERVER] === create_issue CALLED ===")
    print("[SERVER] create_issue args → project_key:", project_key)
    print("[SERVER] create_issue args → summary:", summary)
    print("[SERVER] create_issue args → description:", description)
    print("[SERVER] create_issue args → issue_type:", issue_type)
    print("[SERVER] create_issue args → dna_envelope TYPE:", type(dna_envelope))

    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    if original_message is None:
        original_message = json.dumps(
            {
                "tool": "create_issue",
                "project_key": project_key,
                "summary": summary,
                "description": description,
                "issue_type": issue_type,
            }
        )

    url = f"{JIRA_BASE_URL}/rest/api/3/issue"

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "description": _to_adf(description),  
            "issuetype": {"name": issue_type},
        }
    }

    print("[SERVER] create_issue: Jira URL:", url)
    print("[SERVER] create_issue: payload:", payload)

    resp = requests.post(url, auth=_jira_auth(), json=payload)
    print("[SERVER] create_issue: Jira status code:", resp.status_code)

    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        raise RuntimeError(f"Jira create_issue failed ({resp.status_code}): {err}")

    jira_payload = json.dumps(resp.json(), indent=2)
    print("[SERVER] create_issue: Jira response:", jira_payload)

    return _build_signed_response(original_message, jira_payload, host_block, trust_issues, inject_fake=inject_fake)


@mcp.tool()
async def add_comment(
    issue_key: str,
    comment: str,
    dna_envelope: dict | str | None = None,
    inject_fake: bool = False, 
) -> str:
    print("\n[SERVER] === add_comment CALLED ===")

    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    if original_message is None:
        original_message = json.dumps(
            {"tool": "add_comment", "issue_key": issue_key, "comment": comment}
        )

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/comment"
    payload = {"body": comment}
    print("[SERVER] add_comment: Jira URL:", url)
    print("[SERVER] add_comment: payload:", payload)

    resp = requests.post(url, auth=_jira_auth(), json=payload)
    print("[SERVER] add_comment: Jira status code:", resp.status_code)
    resp.raise_for_status()
    jira_payload = json.dumps(resp.json(), indent=2)

    return _build_signed_response(original_message, jira_payload, host_block, trust_issues, inject_fake=inject_fake)

@mcp.tool()
async def transition_issue(
    issue_key: str,
    transition_name: str,
    dna_envelope: dict | str | None = None,
    inject_fake: bool = False, 
) -> str:
    print("\n[SERVER] === transition_issue CALLED ===")

    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    if original_message is None:
        original_message = json.dumps(
            {
                "tool": "transition_issue",
                "issue_key": issue_key,
                "transition_name": transition_name,
            }
        )

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/transitions"
    print("[SERVER] transition_issue: Jira URL (list transitions):", url)

    resp = requests.get(url, auth=_jira_auth())
    print("[SERVER] transition_issue: list transitions status:", resp.status_code)
    resp.raise_for_status()
    data = resp.json()
    transitions = data.get("transitions", [])

    transition_id = None
    for t in transitions:
        if t.get("name") == transition_name:
            transition_id = t["id"]
            break

    if not transition_id:
        jira_payload = json.dumps(
            {
                "error": f"Transition '{transition_name}' not found",
                "available": [t["name"] for t in transitions],
            },
            indent=2,
        )
        return _build_signed_response(original_message, jira_payload, host_block, trust_issues, inject_fake=inject_fake)
    
    do_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/transitions"
    payload = {"transition": {"id": transition_id}}
    print("[SERVER] transition_issue: Jira URL (do transition):", do_url)
    print("[SERVER] transition_issue: payload:", payload)

    do_resp = requests.post(do_url, auth=_jira_auth(), json=payload)
    print("[SERVER] transition_issue: do transition status:", do_resp.status_code)
    do_resp.raise_for_status()
    jira_payload = json.dumps(
        {"issue": issue_key, "transitioned_to": transition_name},
        indent=2,
    )

    return _build_signed_response(original_message, jira_payload, host_block, trust_issues, inject_fake=inject_fake)

if __name__ == "__main__":
    mcp.run(transport="stdio")
