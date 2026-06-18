# server.py
import os
import sys
import uuid
import builtins
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

# Pure-remote agent — never writes to chain, so enable_nft=False skips deploy.
dna = AgentDNA(alias="GoogleSheetsMCP", api_key=AGENTDNA_API_KEY, chain_url=CHAIN_URL, enable_nft=False)
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


# ---------------- MCP tools ----------------
# Each tool follows the same shape:
#   1. ctx = await dna.handle(dna_envelope)
#   2. business logic
#   3. return dna.build(payload, ctx=ctx)


@mcp.tool()
async def append_task(
    title: str,
    owner: str = "",
    notes: str = "",
    dna_envelope: dict | str | None = None,
) -> str:
    print("\n[SERVER] === append_task CALLED ===")
    ctx = await dna.handle(dna_envelope)

    _ensure_header()

    task_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    row = [task_id, title, "open", owner or "", notes or "", created_at]
    updated_range = _append(f"{SHEET_NAME}!A:F", [row])

    return dna.build({
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
    }, ctx=ctx)


@mcp.tool()
async def get_open_tasks(
    owner: str = "",
    dna_envelope: dict | str | None = None,
) -> str:
    print("\n[SERVER] === get_open_tasks CALLED ===")
    ctx = await dna.handle(dna_envelope)

    _ensure_header()
    values = _read(f"{SHEET_NAME}!A1:F1000")
    tasks = _rows_to_tasks(values)

    want_owner = _norm(owner) if owner else ""
    out = [
        t for t in tasks
        if _norm(t.get("status", "")) == "open"
        and (not want_owner or _norm(t.get("owner", "")) == want_owner)
    ]

    return dna.build(
        {"ok": True, "tasks": out, "action_executed": True},
        ctx=ctx,
    )


@mcp.tool()
async def get_tasks(
    status: str = "",
    owner: str = "",
    dna_envelope: dict | str | None = None,
) -> str:
    print("\n[SERVER] === get_tasks CALLED ===")
    ctx = await dna.handle(dna_envelope)

    _ensure_header()
    values = _read(f"{SHEET_NAME}!A1:F1000")
    tasks = _rows_to_tasks(values)

    want_status = _norm(status) if status else ""
    want_owner = _norm(owner) if owner else ""
    out = [
        t for t in tasks
        if (not want_status or _norm(t.get("status", "")) == want_status)
        and (not want_owner or _norm(t.get("owner", "")) == want_owner)
    ]

    return dna.build(
        {"ok": True, "tasks": out, "action_executed": True},
        ctx=ctx,
    )


@mcp.tool()
async def find_tasks(
    query: str,
    status: str = "",
    owner: str = "",
    dna_envelope: dict | str | None = None,
) -> str:
    print("\n[SERVER] === find_tasks CALLED ===")
    ctx = await dna.handle(dna_envelope)

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
        hay = _norm(t.get("title", "")) + " " + _norm(t.get("notes", ""))
        if all(w in hay for w in words):
            out.append(t)

    return dna.build(
        {"ok": True, "tasks": out, "action_executed": True},
        ctx=ctx,
    )


@mcp.tool()
async def update_task_status(
    task_id: str,
    status: str,
    dna_envelope: dict | str | None = None,
) -> str:
    print("\n[SERVER] === update_task_status CALLED ===")
    ctx = await dna.handle(dna_envelope)

    _ensure_header()

    status_norm = _norm(status)
    if status_norm not in {"open", "done"}:
        return dna.build(
            {"ok": False, "error": "status must be one of: open, done", "action_executed": False},
            ctx=ctx,
        )

    row_num = _find_task_row_by_id(task_id)
    if row_num is None:
        return dna.build(
            {"ok": False, "error": f"task_id not found: {task_id}", "action_executed": False},
            ctx=ctx,
        )

    cell = f"{SHEET_NAME}!C{row_num}"
    _update(cell, [[status_norm]])

    return dna.build(
        {
            "ok": True,
            "task_id": task_id,
            "status": status_norm,
            "updated_cell": cell,
            "action_executed": True,
        },
        ctx=ctx,
    )


@mcp.tool()
async def update_task(
    task_id: str,
    owner: str = "",
    title: str = "",
    notes: str = "",
    dna_envelope: dict | str | None = None,
) -> str:
    print("\n[SERVER] === update_task CALLED ===")
    ctx = await dna.handle(dna_envelope)

    _ensure_header()

    row_num = _find_task_row_by_id(task_id)
    if row_num is None:
        return dna.build(
            {"ok": False, "error": f"task_id not found: {task_id}", "action_executed": False},
            ctx=ctx,
        )

    # HEADER: id(A), title(B), status(C), owner(D), notes(E), created_at(F)
    fields = (("title", "B", title), ("owner", "D", owner), ("notes", "E", notes))
    updated: Dict[str, str] = {}
    for name, col, val in fields:
        if val:
            _update(f"{SHEET_NAME}!{col}{row_num}", [[val]])
            updated[name] = val

    if not updated:
        return dna.build(
            {"ok": False, "error": "No fields to update provided", "action_executed": False},
            ctx=ctx,
        )

    return dna.build(
        {"ok": True, "task_id": task_id, "updated": updated, "action_executed": True},
        ctx=ctx,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
