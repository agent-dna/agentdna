import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import streamlit as st

from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp import ClientSession, StdioServerParameters, types as mcp_types

from pathlib import Path

from agentdna import AgentDNA, NodeClient
from rubix.client import RubixClient
from rubix.querier import Querier

ROOT = Path(__file__).parent
SERVER_PATH = str((ROOT / "server.py").resolve())

SIMULATE_TAMPER = False

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")


CHAIN_URL = os.environ.get("CHAIN_URL")
if not CHAIN_URL:
    raise RuntimeError("Missing CHAIN_URL")

# Host AgentDNA (assumes your AgentDNA handler is now using Path.home()/.agentdna)
dna = AgentDNA(alias="gSheets_Host", role="host", api_key=AGENTDNA_API_KEY, chain_url=CHAIN_URL)
node = NodeClient(alias="gsheets_host")
DEFAULT_BASE_URL = node.get_base_url()

REMOTE_NAME = os.environ.get("AGENTDNA_REMOTE_NAME", "gsheets_server")


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


def _extract_signed_text(tool_output_text: str) -> str:
    t = (tool_output_text or "").strip()
    if not t:
        return t
    try:
        obj = json.loads(t)
        if isinstance(obj, dict) and "combined_json" in obj:
            return obj["combined_json"]
    except Exception:
        pass
    return t


def extract_verified_response(trust_result: dict) -> dict | list | None:
    if not isinstance(trust_result, dict):
        return None

    payload = trust_result.get("payload") or {}

    resp = payload.get("response")
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except Exception:
            return {"raw_response": resp}
    if isinstance(resp, (dict, list)):
        return resp

    env = payload.get("envelope")
    if isinstance(env, dict):
        r = env.get("response")
        if isinstance(r, str):
            try:
                return json.loads(r)
            except Exception:
                return {"raw_response": r}
        if isinstance(r, (dict, list)):
            return r

    return None


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
    Updated tamper simulation design:

    - Host signs a canonical message (host_msg) that includes user_query_raw.
    - Host ALSO sends a transport field 'user_query' to the server.
    - If inject_fake is enabled, ONLY the transport field is tampered.
    - Server should compare transport user_query vs signed user_query_raw and REFUSE to execute tool on mismatch.
    - Host verifies the signed server response via dna.handle() and records it on-chain (execute_nft=True).
    """
    user_query_raw = user_query or tool_name

    # ✅ Signed canonical message (truth source)
    host_msg = {
        "user_query_raw": user_query_raw,   # <- raw / canonical user request
        "tool_name": tool_name,
        "tool_args": tool_args,
    }

    built = dna.build(
        original_message=json.dumps(host_msg),
        state={"channel": "mcp_gsheets"},
    )
    dna_envelope = built["host_json"]

    # ✅ Transport args to MCP server
    args_with_dna = {
        **tool_args,
        "dna_envelope": dna_envelope,

        # IMPORTANT: This is what the server will compare against signed user_query_raw
        "user_query": user_query_raw,
    }

    # ✅ Simulate in-transit tampering: change ONLY the transport field (NOT the signed envelope)
    if SIMULATE_TAMPER:
        args_with_dna["user_query"] = user_query_raw + " [TAMPERED_IN_TRANSIT]"

    tool_output_text = run(mcp_call_raw(tool_name, args_with_dna))
    signed_text = _extract_signed_text(tool_output_text)

    trust_result = run(
        dna.handle(
            resp_parts=[{"text": signed_text}],
            original_task=json.dumps(host_msg),
            remote_name=REMOTE_NAME,
            execute_nft=True,
        )
    )

    verified = extract_verified_response(trust_result)
    trust_issues = trust_result.get("trust_issues") if isinstance(trust_result, dict) else None

    handler = getattr(dna, "handler", None)
    verification_status = getattr(handler, "last_verification_status", "unknown") if handler else "unknown"

    return {
        "ok": True,
        "tool_output_text": tool_output_text,
        "signed_text": signed_text,
        "trust_result": trust_result,
        "verification_status": verification_status,
        "trust_issues": trust_issues or [],
        "verified_payload": verified,
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
        return {"action": "tool", "tool": "append_task", "args": {"title": title, "owner": "", "notes": ""}}

    # Show open tasks
    if "open tasks" in tl or "show open tasks" in tl or tl.strip() in {"open", "open task", "open tasks"}:
        return {"action": "tool", "tool": "get_open_tasks", "args": {}}

    # Show all tasks
    if "show all tasks" in tl or tl.strip() == "all tasks" or tl.strip() == "show tasks":
        return {"action": "tool", "tool": "get_tasks", "args": {}}

    # Show done tasks
    if "done tasks" in tl or "completed tasks" in tl:
        return {"action": "tool", "tool": "get_tasks", "args": {"status": "done"}}

    # Mark done by UUID
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", tl)
    if m and ("done" in tl or "complete" in tl):
        return {
            "action": "tool",
            "tool": "update_task_status",
            "args": {"task_id": m.group(1), "status": "done"},
        }

    # Mark done by title phrase
    m2 = re.search(r"^(update|mark)\s+(.+?)\s+(to\s+)?(done|complete|completed)$", tl)
    if m2:
        phrase = m2.group(2).strip()
        return {"action": "tool", "tool": "find_tasks", "args": {"query": phrase, "status": "open"}}

    return {"action": "chat", "message": "Try: 'Add: finish report', 'Show open tasks', 'Show all tasks', or 'Mark <task_id> done'."}


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
    try:
        return dna.handler.nft_token
    except Exception:
        return ""


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

with st.sidebar:
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
            st.warning("No NFT token available (dna.handler.nft_token not set)")
        else:
            with st.spinner("Fetching NFT data…"):
                nft_resp = fetch_nft_data(nft_id, latest=False)

            def decode_nft_state(state: dict) -> dict:
                state = dict(state)
                nft_data = state.get("NFTData")
                if isinstance(nft_data, str):
                    try:
                        state["NFTData"] = json.loads(nft_data)
                    except Exception:
                        pass
                return state

            if isinstance(nft_resp, list):
                decoded = [decode_nft_state(s) for s in nft_resp]
            elif isinstance(nft_resp, dict):
                decoded = decode_nft_state(nft_resp)
            else:
                decoded = nft_resp

            pretty = json.dumps(decoded, indent=2)
            st.session_state.messages.append(
                {"role": "agent", "content": f"NFT history for {nft_id}:\n\n```json\n{pretty}\n```"}
            )
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

            # If server refused due to tamper, vp typically contains tamper report
            # (depends on your server.py implementation)
            if isinstance(vp, dict) and vp.get("tampered"):
                st.error("🚨 Tampering detected. No action executed.")
                st.json(vp)
                st.session_state.messages.append({"role": "assistant", "content": "Tampering detected; request refused."})
                st.rerun()

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