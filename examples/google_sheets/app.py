import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import streamlit as st

from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp import ClientSession, StdioServerParameters, types as mcp_types

from pathlib import Path

from agentdna import AgentDNA, resolve_chain_url
from rubix.client import RubixClient
from rubix.querier import Querier

ROOT = Path(__file__).parent
SERVER_PATH = str((ROOT / "server.py").resolve())

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")


CHAIN_URL = os.environ.get("CHAIN_URL")
if not CHAIN_URL:
    raise RuntimeError("Missing CHAIN_URL")

# Host AgentDNA — pure signer, never writes to chain (enable_nft=False).
# The user (constructed below from a sidebar alias) owns the audit-log NFT.
host_dna = AgentDNA(
    alias="GoogleSheetsAgent",
    api_key=AGENTDNA_API_KEY,
    chain_url=CHAIN_URL,
    kind="agent",
    enable_nft=False,
)
DEFAULT_BASE_URL = resolve_chain_url()

REMOTE_NAME = os.environ.get("AGENTDNA_REMOTE_NAME", "GoogleSheetsMCP")
DEFAULT_USER_ALIAS = "GoogleSheetsAgent_USER"


def _server_params() -> StdioServerParameters:
    env_vars = dict(os.environ)

    # ✅ single canonical location in HOME dir (same as host)
    env_vars["AGENTDNA_HOME"] = str((Path.home() / ".agentdna").resolve())

    if "GOOGLE_APPLICATION_CREDENTIALS" in env_vars:
        p = env_vars["GOOGLE_APPLICATION_CREDENTIALS"]
        if p and not os.path.isabs(p):
            env_vars["GOOGLE_APPLICATION_CREDENTIALS"] = str((ROOT / p).resolve())

    return StdioServerParameters(
        command=sys.executable,
        args=[SERVER_PATH],
        env=env_vars,
    )


def run(coro):
    return asyncio.run(coro)


def _tool_result_to_text(tool_result) -> str:
    parts: list[str] = []
    for block in tool_result.content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        else:
            parts.append(str(block))
    return "\n".join(parts).strip()


async def mcp_call_raw(tool_name: str, tool_args: Dict[str, Any]) -> str:
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_result = await session.call_tool(tool_name, tool_args)
            return _tool_result_to_text(tool_result)


async def mcp_list_tools() -> List[str]:
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]


def trusted_mcp_call(tool_name: str, tool_args: Dict[str, Any], user_query: str = "") -> Dict[str, Any]:
    """
    Full user → host → MCP delegation chain:
      - user_dna.build(intent)               → signed user intent
      - host_dna.build(host_msg, parent=…)   → host envelope wrapping the user's signed block
      - user_dna.handle(reply, original=)    → typed VerifyResult + audit-log NFT
    """
    user_dna = st.session_state.user_dna

    user_signed = user_dna.build({
        "intent": user_query or tool_name,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    host_msg = {
        "user_query": user_query or tool_name,
        "tool_name": tool_name,
        "tool_args": tool_args,
    }
    env = host_dna.build(host_msg, parent=user_signed)
    args_with_dna = {**tool_args, "dna_envelope": str(env)}

    tool_output_text = run(mcp_call_raw(tool_name, args_with_dna))
    result = run(user_dna.handle(tool_output_text, original=env, remote_name=REMOTE_NAME))

    return {
        "tool_output_text":    tool_output_text,
        "verified_payload":    result.payload,
        "verification_status": result.verification_status,
        "trust_issues":        result.trust_issues,
    }


def decide_action(user_text: str):
    t = user_text.strip()
    tl = t.lower().strip()

    # Add task
    if (
        tl.startswith("add ")
        or tl.startswith("add:")
        or tl.startswith("task:")
        or "add task" in tl
        or "create task" in tl
        or "new task" in tl
        or tl.startswith("append ")
    ):
        title = t.split(":", 1)[1].strip() if ":" in t else re.sub(r"^(add|append|task)\s*", "", t, flags=re.I).strip()
        if not title:
            return {"action": "chat", "message": "What should the task title be?"}

        owner = ""
        owner_m = re.search(r"\s+(?:owner|by|assigned\s+to)[:\s]+([^\s,]+)", title, flags=re.I)
        if owner_m:
            owner = owner_m.group(1).strip()
            title = title[:owner_m.start()].strip()

        return {"action": "tool", "tool": "append_task", "args": {"title": title, "owner": owner, "notes": ""}}

    # Show open tasks
    if "open tasks" in tl or "show open tasks" in tl or tl.strip() in {"open", "open task", "open tasks"}:
        return {"action": "tool", "tool": "get_open_tasks", "args": {}}

    # Show all tasks
    if "show all tasks" in tl or tl.strip() == "all tasks" or tl.strip() == "show tasks":
        return {"action": "tool", "tool": "get_tasks", "args": {}}

    # Show done tasks
    if "done tasks" in tl or "completed tasks" in tl:
        return {"action": "tool", "tool": "get_tasks", "args": {"status": "done"}}

    # Update owner of a task: "set owner of <title/id> to <name>"
    m_owner = re.search(
        r"(?:set|update|assign)\s+owner\s+(?:of\s+)?(.+?)\s+to\s+(\S+)", tl
    )
    if m_owner:
        phrase, new_owner = m_owner.group(1).strip(), m_owner.group(2).strip()
        uuid_m = re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", phrase)
        if uuid_m:
            return {"action": "tool", "tool": "update_task", "args": {"task_id": phrase, "owner": new_owner}}
        return {"action": "tool", "tool": "find_and_update_task", "args": {"query": phrase, "owner": new_owner}}

    # Mark done by UUID
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", tl)
    if m and ("done" in tl or "complete" in tl):
        return {
            "action": "tool",
            "tool": "update_task_status",
            "args": {"task_id": m.group(1), "status": "done"},
        }

    # Mark done by title phrase — catches:
    #   "mark <title> done", "update <title> to done", "<title> done", "complete <title>"
    m2 = re.search(
        r"^(?:(?:mark|update|complete|finish|close)\s+)?(.+?)\s+(?:to\s+)?(?:done|complete|completed|finished)$",
        tl,
    ) or re.search(
        r"^(?:complete|finish|close)\s+(.+)$",
        tl,
    )
    if m2:
        phrase = m2.group(1).strip()
        # avoid false-positives from pure "done tasks" / "show" queries already handled above
        if phrase not in {"tasks", "all tasks", "open tasks"}:
            return {"action": "tool", "tool": "find_tasks", "args": {"query": phrase, "status": "open"}}

    return {"action": "chat", "message": "Try: 'Add: finish report', 'Show open tasks', 'Mark <title> done', or 'Set owner of <title> to <name>'."}


def fetch_open_tasks():
    out = trusted_mcp_call("get_open_tasks", {}, user_query="show open tasks")
    vp = out.get("verified_payload") or {}
    tasks = vp.get("tasks", []) if isinstance(vp, dict) else (vp if isinstance(vp, list) else [])
    if isinstance(tasks, dict):
        tasks = [tasks]
    st.session_state["open_tasks"] = tasks


def fetch_tasks(status: str = ""):
    args = {}
    if status:
        args["status"] = status
    out = trusted_mcp_call("get_tasks", args, user_query=f"show tasks status={status or 'all'}")
    vp = out.get("verified_payload") or {}
    tasks = vp.get("tasks", []) if isinstance(vp, dict) else (vp if isinstance(vp, list) else [])
    if isinstance(tasks, dict):
        tasks = [tasks]
    st.session_state["tasks"] = tasks


def get_nft_token_from_host() -> str:
    user_dna = st.session_state.get("user_dna")
    return getattr(user_dna, "nft_token", None) or ""


def fetch_nft_data(nft_id: str, latest: bool = False) -> Any:
    client = RubixClient(node_url=DEFAULT_BASE_URL, timeout=300)
    q = Querier(client)
    return q.get_nft_states(nft_address=nft_id, only_latest_state=latest)


st.set_page_config(page_title="MCP Sheets Task Agent", page_icon="✅")
st.title("MCP Google Sheets Task Agent")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "open_tasks" not in st.session_state:
    st.session_state.open_tasks = []
if "tasks" not in st.session_state:
    st.session_state.tasks = []
if "view" not in st.session_state:
    st.session_state.view = "open"
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []
if "user_alias" not in st.session_state:
    st.session_state.user_alias = DEFAULT_USER_ALIAS


def _decode_nft_state(state: dict) -> dict:
    """Decode the NFTData JSON-string field into a dict for nicer rendering."""
    state = dict(state)
    nft_data = state.get("NFTData")
    if isinstance(nft_data, str):
        try:
            state["NFTData"] = json.loads(nft_data)
        except Exception:
            pass
    return state

with st.sidebar:
    st.subheader("Signed in as")
    new_alias = st.text_input(
        "User alias",
        value=st.session_state.user_alias,
        help="Your chain identity. Each unique alias gets its own DID + audit-log NFT.",
    )
    if new_alias != st.session_state.user_alias or "user_dna" not in st.session_state:
        st.session_state.user_alias = new_alias
        st.session_state.user_dna = AgentDNA(
            alias=new_alias,
            api_key=AGENTDNA_API_KEY,
            chain_url=CHAIN_URL,
            kind="user",
        )

    st.divider()

    st.subheader("Controls")

    st.session_state.view = st.radio(
        "View",
        options=["open", "all", "done"],
        format_func=lambda v: {"open": "Open tasks", "all": "All tasks", "done": "Done tasks"}[v],
        index=["open", "all", "done"].index(st.session_state.view),
    )

    if st.button("🔄 Refresh"):
        if st.session_state.view == "open":
            fetch_open_tasks()
        elif st.session_state.view == "done":
            fetch_tasks(status="done")
        else:
            fetch_tasks(status="")
        st.rerun()

    if st.button("List MCP tools"):
        tools = run(mcp_list_tools())
        st.write(tools)

    st.divider()

    nft_id = get_nft_token_from_host()
    st.caption(f"NFT: {nft_id or '(none)'}")

    if st.button("History Records"):
        if not nft_id:
            st.warning("No NFT token available — run a tool first so the audit-log NFT is registered.")
        else:
            with st.spinner("Fetching NFT data…"):
                nft_resp = fetch_nft_data(nft_id, latest=False)
            if isinstance(nft_resp, list):
                st.session_state.chain_history = [_decode_nft_state(s) for s in nft_resp]
            elif isinstance(nft_resp, dict):
                st.session_state.chain_history = [_decode_nft_state(nft_resp)]
            else:
                st.session_state.chain_history = []
            st.rerun()

if "last_update" in st.session_state:
    lu = st.session_state.pop("last_update")
    upd = lu["upd"]
    if isinstance(upd, dict) and upd.get("ok"):
        st.success(f"Marked done: {lu['title']}")
        st.caption(upd)
    else:
        st.error(f"Update failed: {upd}")

if st.session_state.view == "open" and not st.session_state.open_tasks:
    fetch_open_tasks()
if st.session_state.view in {"all", "done"} and not st.session_state.tasks:
    fetch_tasks(status="done" if st.session_state.view == "done" else "")

# chain-history panel — foldable JSON tree, wraps long values (DIDs, sigs,
# the signed response payload) instead of forcing horizontal scroll.
if st.session_state.chain_history:
    with st.expander(
        f"Chain History — NFT `{nft_id}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.json(st.session_state.chain_history, expanded=2)

# chat history
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.write(m["content"])

if st.session_state.view == "open":
    st.subheader("Open tasks")
    tasks = st.session_state.open_tasks
elif st.session_state.view == "done":
    st.subheader("Done tasks")
    tasks = st.session_state.tasks
else:
    st.subheader("All tasks")
    tasks = st.session_state.tasks

if not tasks:
    st.info("No tasks to show.")
else:
    df = pd.DataFrame(tasks)
    cols = [c for c in ["id", "title", "owner", "notes", "status", "created_at"] if c in df.columns]
    st.dataframe(df[cols] if cols else df, width="stretch")

    if st.session_state.view == "open":
        st.divider()
        st.subheader("Actions")
        for t in tasks:
            tid = t.get("id", "")
            title = t.get("title", "(no title)")
            c1, c2 = st.columns([4, 1])
            c1.write(title)

            if c2.button("Done", key=f"done_{tid}"):
                with st.spinner("Marking done…"):
                    out = trusted_mcp_call(
                        "update_task_status",
                        {"task_id": tid, "status": "done"},
                        user_query=f"mark {title} done",
                    )
                    vp = out.get("verified_payload") or {}
                    upd = vp if isinstance(vp, dict) else {"raw": vp}

                st.session_state["last_update"] = {"title": title, "upd": upd}
                fetch_open_tasks()
                st.rerun()

user_text = st.chat_input("Add a task, list tasks, or mark something done…")

if user_text:
    st.session_state.messages.append({"role": "user", "content": user_text})
    with st.chat_message("user"):
        st.write(user_text)

    decision = decide_action(user_text)

    with st.chat_message("assistant"):
        if decision.get("action") == "chat":
            msg = decision.get("message", "OK.")
            st.write(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})

        elif decision.get("action") == "tool":
            tool = decision["tool"]
            args = decision.get("args", {})

            with st.spinner(f"Calling {tool}…"):
                out = trusted_mcp_call(tool, args, user_query=user_text)
                vp = out.get("verified_payload")

            if tool == "append_task" and isinstance(vp, dict):
                task = vp.get("task", {})
                st.success(f"Added: {task.get('title', '(no title)')}")
                fetch_open_tasks()

            elif tool == "get_open_tasks":
                fetch_open_tasks()
                st.session_state.view = "open"

            elif tool == "get_tasks":
                status = (args or {}).get("status", "")
                fetch_tasks(status=status)
                st.session_state.view = "done" if status == "done" else "all"

            elif tool == "update_task_status":
                if isinstance(vp, dict) and vp.get("ok"):
                    st.success("Updated.")
                else:
                    st.error(vp)
                fetch_open_tasks()

            elif tool == "update_task":
                if isinstance(vp, dict) and vp.get("ok"):
                    updated = vp.get("updated", {})
                    st.success(f"Task updated: {updated}")
                else:
                    st.error(vp)
                fetch_open_tasks()

            elif tool == "find_and_update_task":
                # Find by title then update owner
                query = args.get("query", "")
                new_owner = args.get("owner", "")
                with st.spinner("Searching for task…"):
                    find_out = trusted_mcp_call("find_tasks", {"query": query}, user_query=user_text)
                    find_vp = find_out.get("verified_payload") or {}
                    matches = find_vp.get("tasks", []) if isinstance(find_vp, dict) else (find_vp if isinstance(find_vp, list) else [])

                if not matches:
                    st.info("No matching tasks found.")
                elif len(matches) == 1:
                    tid = matches[0].get("id")
                    title = matches[0].get("title", "(no title)")
                    with st.spinner("Updating owner…"):
                        upd_out = trusted_mcp_call(
                            "update_task",
                            {"task_id": tid, "owner": new_owner},
                            user_query=user_text,
                        )
                        upd_vp = upd_out.get("verified_payload") or {}
                    if isinstance(upd_vp, dict) and upd_vp.get("ok"):
                        st.success(f"Owner of '{title}' set to '{new_owner}'.")
                    else:
                        st.error(upd_vp)
                    fetch_open_tasks()
                else:
                    st.write("Multiple matches — which one?")
                    mdf = pd.DataFrame(matches)
                    mcols = [c for c in ["id", "title", "owner", "status"] if c in mdf.columns]
                    st.dataframe(mdf[mcols] if mcols else mdf, width="stretch")

            elif tool == "find_tasks":
                matches = []
                if isinstance(vp, dict) and isinstance(vp.get("tasks"), list):
                    matches = vp["tasks"]
                elif isinstance(vp, list):
                    matches = vp

                if not matches:
                    st.info("No matching open tasks found.")
                elif len(matches) == 1:
                    tid = matches[0].get("id")
                    title = matches[0].get("title", "(no title)")
                    out2 = trusted_mcp_call(
                        "update_task_status",
                        {"task_id": tid, "status": "done"},
                        user_query=f"mark {title} done",
                    )
                    vp2 = out2.get("verified_payload") or {}
                    upd = vp2 if isinstance(vp2, dict) else {"raw": vp2}
                    st.session_state["last_update"] = {"title": title, "upd": upd}
                else:
                    st.write("Which one did you mean?")
                    mdf = pd.DataFrame(matches)
                    mcols = [c for c in ["id", "title", "owner", "notes", "status", "created_at"] if c in mdf.columns]
                    st.dataframe(mdf[mcols] if mcols else mdf, width="stretch")

            else:
                st.json(vp)

            st.session_state.messages.append({"role": "assistant", "content": "Done."})
            st.rerun()