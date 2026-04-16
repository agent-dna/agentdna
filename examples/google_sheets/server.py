# server.py
import os
import sys
import json
import uuid
import builtins
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from google.oauth2 import service_account
from googleapiclient.discovery import build
from agentdna import AgentDNA

_original_print = builtins.print


def _stderr_print(*args, **kwargs):
    _original_print(
        *args,
        file=sys.stderr,
        **{k: v for k, v in kwargs.items() if k != "file"},
    )


builtins.print = _stderr_print

load_dotenv()
mcp = FastMCP("GoogleSheetsTasks")

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")

CHAIN_URL = os.environ.get("CHAIN_URL", "").strip()

dna = AgentDNA(alias="gSheets_Server", role="remote", api_key=AGENTDNA_API_KEY, chain_url=CHAIN_URL)
print("[SERVER] Sheets MCP server DID:", dna.trust.did)
print("[SERVER] Sheets MCP server base URL:", dna.trust.base_url)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SPREADSHEET_ID = os.getenv("GSHEETS_SPREADSHEET_ID", "").strip()
SHEET_NAME = os.getenv("GSHEETS_SHEET_NAME", "Sheet1").strip()
HEADER = ["id", "title", "status", "owner", "notes", "created_at"]

if not SPREADSHEET_ID:
    raise RuntimeError("GSHEETS_SPREADSHEET_ID is not set")


def _abs_cred_path() -> str:
    p = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials/service_account.json")
    if os.path.isabs(p):
        return p
    return str((Path(__file__).parent / p).resolve())


def _svc():
    key_path = _abs_cred_path()
    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _read(a1_range: str) -> List[List[Any]]:
    service = _svc()
    resp = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=a1_range,
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
    )
    return resp.get("values", [])


def _update(a1_range: str, values: List[List[Any]]) -> None:
    service = _svc()
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=a1_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def _append(range_a1: str, values: List[List[Any]]) -> str:
    service = _svc()
    resp = service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=range_a1,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    updates = resp.get("updates", {})
    return updates.get("updatedRange", range_a1)


def _ensure_header():
    rows = _read(f"{SHEET_NAME}!A1:F1")
    if not rows:
        _update(f"{SHEET_NAME}!A1:F1", [HEADER])
        return

    existing = [str(x).strip().lower() for x in rows[0]]
    expected = [h.lower() for h in HEADER]
    if existing != expected:
        raise ValueError(
            f"Header row mismatch in {SHEET_NAME}. Expected: {HEADER} | Found: {rows[0]}"
        )


def _rows_to_tasks(values: List[List[Any]]) -> List[Dict[str, Any]]:
    if not values:
        return []
    header = [str(x).strip() for x in values[0]]
    tasks = []
    for r in values[1:]:
        row = r + [""] * (len(header) - len(r))
        task = {header[i]: row[i] for i in range(len(header))}
        tasks.append(task)
    return tasks


def _find_task_row_by_id(task_id: str, max_rows: int = 1000) -> Optional[int]:
    values = _read(f"{SHEET_NAME}!A2:A{max_rows}")
    for idx, row in enumerate(values, start=2):
        if row and str(row[0]).strip() == task_id.strip():
            return idx
    return None


def _norm(x: Any) -> str:
    return str(x).strip().lower()


def _parse_signed_host_msg(original_message: Optional[str]) -> Dict[str, Any]:
    """
    The host signs json.dumps(host_msg). We parse it to extract user_query_raw etc.
    If parsing fails, return {}.
    """
    if not original_message or not isinstance(original_message, str):
        return {}
    try:
        obj = json.loads(original_message)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


async def _verify_host_envelope(
    dna_envelope: Optional[Any],
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[list]]:
    """
    Returns:
      original_message: the signed JSON string from host (json.dumps(host_msg))
      host_block: host signature block
      trust_issues: list of issues (may be empty)
    """
    print("[SERVER] verify_host_envelope: raw dna_envelope TYPE:", type(dna_envelope))

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

    print(
        "[SERVER] verify_host_envelope: info keys:",
        list(info.keys()) if isinstance(info, dict) else type(info),
    )

    original_message = info.get("original_message") if isinstance(info, dict) else None
    host_block = info.get("host_block") if isinstance(info, dict) else None
    trust_issues = info.get("trust_issues") if isinstance(info, dict) else None

    return original_message, host_block, trust_issues


def _build_signed_response(
    original_message: Optional[str],
    payload_obj: Any,
    host_block: Optional[Dict[str, Any]],
    trust_issues: Optional[list],
) -> str:
    """
    Build signed response string (combined_json).

    IMPORTANT:
    - We must sign with the SAME original_message string that the host signed/expected
      (json.dumps(host_msg)), otherwise host-side verification will flag mismatch.
    """
    payload_str = json.dumps(payload_obj, separators=(",", ":"), sort_keys=True)

    if original_message is None:
        # Fallback only; in normal flow we want the host-provided original_message
        original_message = payload_str

    built = dna.build(
        original_message=original_message,
        response=payload_str,
        host_block=host_block,
        extra={"host_trust_issues": trust_issues},
    )

    if isinstance(built, dict) and "combined_json" in built:
        return built["combined_json"]

    if isinstance(built, str):
        return built

    return json.dumps(built)


def _tamper_refusal_response(
    *,
    original_message: Optional[str],
    host_block: Optional[Dict[str, Any]],
    trust_issues: Optional[list],
    signed_user_query: str,
    received_user_query: str,
) -> str:
    issues = list(trust_issues or [])
    issues.append("User query tampering detected: transport user_query != signed user_query_raw")

    payload = {
        "ok": False,
        "tampered": True,
        "reason": "User query mismatch vs signed envelope",
        "signed_user_query": signed_user_query,
        "received_user_query": received_user_query,
        "action_executed": False,
    }
    return _build_signed_response(original_message, payload, host_block, issues)


def _check_user_query_tamper(
    *,
    original_message: Optional[str],
    host_block: Optional[Dict[str, Any]],
    trust_issues: Optional[list],
    received_user_query: Optional[str],
) -> Optional[str]:
    """
    Returns:
      - None if OK to proceed (or not enough info to check)
      - Signed refusal combined_json string if tampering detected
    """
    if not original_message or not received_user_query:
        # Can't compare; proceed.
        return None

    signed_msg = _parse_signed_host_msg(original_message)
    signed_user_query = signed_msg.get("user_query_raw") or signed_msg.get("user_query")

    if not isinstance(signed_user_query, str) or not signed_user_query:
        # No signed field; proceed.
        return None

    if str(received_user_query) != signed_user_query:
        print("[SERVER] 🚨 Tamper detected!")
        print("[SERVER] signed_user_query:", signed_user_query)
        print("[SERVER] received_user_query:", received_user_query)
        return _tamper_refusal_response(
            original_message=original_message,
            host_block=host_block,
            trust_issues=trust_issues,
            signed_user_query=signed_user_query,
            received_user_query=str(received_user_query),
        )

    return None


# ---------------- MCP tools ----------------

@mcp.tool()
async def append_task(
    title: str,
    owner: str = "",
    notes: str = "",
    dna_envelope: dict | str | None = None,
    user_query: str = "",
    inject_fake: bool = False,  # kept for backward compatibility; no longer used
) -> str:
    print("\n[SERVER] === append_task CALLED ===")
    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    # ✅ Refuse early if tampered (NO SHEETS ACTIONS)
    refusal = _check_user_query_tamper(
        original_message=original_message,
        host_block=host_block,
        trust_issues=trust_issues,
        received_user_query=user_query,
    )
    if refusal is not None:
        return refusal

    if original_message is None:
        original_message = json.dumps(
            {"tool": "append_task", "title": title, "owner": owner, "notes": notes}
        )

    _ensure_header()

    task_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    row = [task_id, title, "open", owner or "", notes or "", created_at]
    updated_range = _append(f"{SHEET_NAME}!A:F", [row])

    payload = {
        "ok": True,
        "task": {
            "id": task_id,
            "title": title,
            "status": "open",
            "owner": owner or "",
            "notes": notes or "",
            "created_at": created_at,
        },
        "updated_range": updated_range,
        "action_executed": True,
    }
    return _build_signed_response(original_message, payload, host_block, trust_issues)


@mcp.tool()
async def get_open_tasks(
    owner: str = "",
    dna_envelope: dict | str | None = None,
    user_query: str = "",
    inject_fake: bool = False,  # kept for backward compatibility; no longer used
) -> str:
    print("\n[SERVER] === get_open_tasks CALLED ===")
    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    # ✅ Refuse early if tampered (NO SHEETS ACTIONS)
    refusal = _check_user_query_tamper(
        original_message=original_message,
        host_block=host_block,
        trust_issues=trust_issues,
        received_user_query=user_query,
    )
    if refusal is not None:
        return refusal

    if original_message is None:
        original_message = json.dumps({"tool": "get_open_tasks", "owner": owner})

    _ensure_header()
    values = _read(f"{SHEET_NAME}!A1:F1000")
    tasks = _rows_to_tasks(values)

    want_owner = _norm(owner) if owner else ""
    out = []
    for t in tasks:
        if _norm(t.get("status", "")) != "open":
            continue
        if want_owner and _norm(t.get("owner", "")) != want_owner:
            continue
        out.append(t)

    payload = {"ok": True, "tasks": out, "action_executed": True}
    return _build_signed_response(original_message, payload, host_block, trust_issues)


@mcp.tool()
async def get_tasks(
    status: str = "",
    owner: str = "",
    dna_envelope: dict | str | None = None,
    user_query: str = "",
    inject_fake: bool = False,  # kept for backward compatibility; no longer used
) -> str:
    print("\n[SERVER] === get_tasks CALLED ===")
    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    # ✅ Refuse early if tampered (NO SHEETS ACTIONS)
    refusal = _check_user_query_tamper(
        original_message=original_message,
        host_block=host_block,
        trust_issues=trust_issues,
        received_user_query=user_query,
    )
    if refusal is not None:
        return refusal

    if original_message is None:
        original_message = json.dumps(
            {"tool": "get_tasks", "status": status, "owner": owner}
        )

    _ensure_header()
    values = _read(f"{SHEET_NAME}!A1:F1000")
    tasks = _rows_to_tasks(values)

    want_status = _norm(status) if status else ""
    want_owner = _norm(owner) if owner else ""

    out = []
    for t in tasks:
        if want_status and _norm(t.get("status", "")) != want_status:
            continue
        if want_owner and _norm(t.get("owner", "")) != want_owner:
            continue
        out.append(t)

    payload = {"ok": True, "tasks": out, "action_executed": True}
    return _build_signed_response(original_message, payload, host_block, trust_issues)


@mcp.tool()
async def find_tasks(
    query: str,
    status: str = "",
    owner: str = "",
    dna_envelope: dict | str | None = None,
    user_query: str = "",
    inject_fake: bool = False,  # kept for backward compatibility; no longer used
) -> str:
    print("\n[SERVER] === find_tasks CALLED ===")
    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    # ✅ Refuse early if tampered (NO SHEETS ACTIONS)
    refusal = _check_user_query_tamper(
        original_message=original_message,
        host_block=host_block,
        trust_issues=trust_issues,
        received_user_query=user_query,
    )
    if refusal is not None:
        return refusal

    if original_message is None:
        original_message = json.dumps(
            {"tool": "find_tasks", "query": query, "status": status, "owner": owner}
        )

    _ensure_header()
    values = _read(f"{SHEET_NAME}!A1:F1000")
    tasks = _rows_to_tasks(values)

    q = _norm(query)
    want_status = _norm(status) if status else ""
    want_owner = _norm(owner) if owner else ""
    words = [w for w in q.split() if w]

    out = []
    for t in tasks:
        if want_status and _norm(t.get("status", "")) != want_status:
            continue
        if want_owner and _norm(t.get("owner", "")) != want_owner:
            continue
        hay = (_norm(t.get("title", "")) + " " + _norm(t.get("notes", "")))
        if all(w in hay for w in words):
            out.append(t)

    payload = {"ok": True, "tasks": out, "action_executed": True}
    return _build_signed_response(original_message, payload, host_block, trust_issues)


@mcp.tool()
async def update_task_status(
    task_id: str,
    status: str,
    dna_envelope: dict | str | None = None,
    user_query: str = "",
    inject_fake: bool = False,  # kept for backward compatibility; no longer used
) -> str:
    print("\n[SERVER] === update_task_status CALLED ===")
    original_message, host_block, trust_issues = await _verify_host_envelope(dna_envelope)

    # ✅ Refuse early if tampered (NO SHEETS ACTIONS)
    refusal = _check_user_query_tamper(
        original_message=original_message,
        host_block=host_block,
        trust_issues=trust_issues,
        received_user_query=user_query,
    )
    if refusal is not None:
        return refusal

    if original_message is None:
        original_message = json.dumps(
            {"tool": "update_task_status", "task_id": task_id, "status": status}
        )

    _ensure_header()

    status_norm = _norm(status)
    if status_norm not in {"open", "done"}:
        payload = {
            "ok": False,
            "error": "status must be one of: open, done",
            "action_executed": False,
        }
        return _build_signed_response(original_message, payload, host_block, trust_issues)

    row_num = _find_task_row_by_id(task_id)
    if row_num is None:
        payload = {
            "ok": False,
            "error": f"task_id not found: {task_id}",
            "action_executed": False,
        }
        return _build_signed_response(original_message, payload, host_block, trust_issues)

    cell = f"{SHEET_NAME}!C{row_num}"
    _update(cell, [[status_norm]])

    payload = {
        "ok": True,
        "task_id": task_id,
        "status": status_norm,
        "updated_cell": cell,
        "action_executed": True,
    }
    return _build_signed_response(original_message, payload, host_block, trust_issues)


if __name__ == "__main__":
    mcp.run(transport="stdio")