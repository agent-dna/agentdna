# app.py
import os
import sys
import json
import asyncio

import streamlit as st
from dotenv import load_dotenv
from google import genai

from agentdna import AgentDNA, resolve_chain_url
from rubix.client import RubixClient
from rubix.querier import Querier

from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client

# ─────────────────────────────
# Env + AgentDNA host setup
# ─────────────────────────────

load_dotenv()

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")

dna = AgentDNA(alias="jira_host", role="host", api_key=AGENTDNA_API_KEY)
print("[HOST] ✅ AgentDNA DID:", dna.trust.did)
print("[HOST] ✅ AgentDNA base URL:", dna.trust.base_url)

DEFAULT_BASE_URL = resolve_chain_url()
print("[HOST] ✅ Rubix node base URL:", DEFAULT_BASE_URL)


def extract_json(raw: str) -> str:
    raw = raw.strip()
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            if "{" in part and "}" in part:
                raw = part
                break
    raw = raw.strip()
    if raw.lower().startswith("json"):
        raw = raw[4:].lstrip()
    return raw


SYSTEM_PROMPT = """
You are a Jira assistant. Use the Jira MCP tools when needed.

Tools:
1) search_issues(jql: string, max_results: int = 10)
2) get_issue(key: string)
3) create_issue(project_key: string, summary: string, description: string, issue_type: string = "Task")
4) add_comment(issue_key: string, comment: string)
5) transition_issue(issue_key: string, transition_name: string)

Rules:
- If a tool is needed, return only JSON: {"tool": "<name>", "args": {...}}
- If no tool is needed, return only JSON: {"answer": "<text>"}
- jql MUST ALWAYS be a plain string. Example:
  "assignee = currentUser() AND status != Done ORDER BY created DESC"
- Do not use markdown code fences.
"""


def init_gemini() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY")
    return genai.Client(api_key=api_key)


# ─────────────────────────────
# NFT helper functions
# ─────────────────────────────

def get_nft_token_from_host() -> str:
    """
    Read the NFT token that AgentDNA is using for this host.
    This is set by the Rubix handler inside AgentDNA.
    """
    try:
        return dna.handler.nft_token
    except Exception:
        return ""


def fetch_nft_data(nft_id: str, latest: bool = False) -> dict:
    """
    Query Rubix for all (or latest) states of this NFT.
    Same pattern as the pickleball Host console.
    """
    client = RubixClient(node_url=DEFAULT_BASE_URL, timeout=300)
    rubix_querier = Querier(client)
    states = rubix_querier.get_nft_states(
        nft_address=nft_id,
        only_latest_state=latest,
    )
    print(f"[HOST] Total NFT States Retrieved: {len(states) if isinstance(states, list) else states}")
    return states


# ─────────────────────────────
# Core agent turn
# ─────────────────────────────

async def run_agent_turn(user_input: str):
    print("\n[HOST] ─────────────────────────────")
    print("[HOST] New turn, user_input:", repr(user_input))

    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        val = os.environ.get(var)
        print(f"[HOST] ENV {var} =", repr(val))
        if not val:
            raise RuntimeError(f"Missing environment variable: {var}")

    env_vars = dict(os.environ)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        env=env_vars,
    )

    client = init_gemini()
    model_id = "gemini-2.5-flash"


    async with stdio_client(server_params) as (read, write):
        print("[HOST] ✅ Connected to MCP server over stdio")
        async with ClientSession(read, write) as session:
            print("[HOST] Calling session.initialize()…")
            await session.initialize()
            print("[HOST] ✅ session.initialize() complete")

            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            print("[HOST] Available MCP tools:", tool_names)

            decision_prompt = f"""{SYSTEM_PROMPT}

User request: {user_input}

Return only one JSON object.
"""
            print("[HOST] Decision prompt:\n", decision_prompt)

            decision_raw = client.models.generate_content(
                model=model_id,
                contents=decision_prompt,
            ).text or ""
            print("[HOST] Raw decision from Gemini:", repr(decision_raw))

            normalized = extract_json(decision_raw)
            print("[HOST] Normalized decision text:", repr(normalized))

            try:
                decision = json.loads(normalized)
            except Exception as e:
                print("[HOST] Failed to parse decision JSON:", e)
                return decision_raw.strip(), None, None, ""

            print("[HOST] Parsed decision object:", decision)

            if "tool" not in decision:
                answer = decision.get("answer", decision_raw)
                return answer.strip(), None, None, ""

            tool_name = decision["tool"]
            tool_args = decision.get("args", {})
            print("[HOST] tool_name:", tool_name)
            print(
                "[HOST] tool_args BEFORE fixes:",
                tool_args,
                "types:",
                {k: type(v) for k, v in tool_args.items()},
            )

            if tool_name == "search_issues":
                if "max_results" not in tool_args:
                    tool_args["max_results"] = 10
                else:
                    try:
                        tool_args["max_results"] = int(tool_args["max_results"])
                    except Exception:
                        tool_args["max_results"] = 10

            print(
                "[HOST] tool_args AFTER fixes:",
                tool_args,
                "types:",
                {k: type(v) for k, v in tool_args.items()},
            )

            host_msg = {
                "user_query": user_input,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
            print("[HOST] host_msg:", host_msg)

            built = dna.build(
                original_message=json.dumps(host_msg),
                state={"channel": "mcp_jira"},
            )
            dna_envelope = built["host_json"]

            if isinstance(dna_envelope, dict):
                dna_envelope = json.dumps(dna_envelope)

            tool_args_with_dna = {
                **tool_args,
                "dna_envelope": dna_envelope,
            }

            from_streamlit = getattr(__import__("streamlit"), "session_state", None)
            inject_fake_flag = False
            if from_streamlit is not None:
                inject_fake_flag = bool(from_streamlit.get("inject_fake", False))

            if inject_fake_flag:
                tool_args_with_dna["inject_fake"] = True

            print(f"[HOST] Calling MCP tool '{tool_name}' with args:", tool_args_with_dna)
            tool_result = await session.call_tool(tool_name, arguments=tool_args_with_dna)
            print("[HOST] MCP tool call completed")

            parts: list[str] = []
            for block in tool_result.content:
                if isinstance(block, mcp_types.TextContent):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            tool_output_text = "\n".join(parts)
            print("[HOST] tool_output_text (snippet):", tool_output_text[:300])

            print("[HOST] Verifying tool response with AgentDNA.handle()…")
            trust_result = await dna.handle(
                resp_parts=[{"text": tool_output_text}],
                original_task=json.dumps(host_msg),
                remote_name="jira_server",
                execute_nft=True,  
            )
            print("[HOST] trust_result:", trust_result)

            handler = getattr(dna, "handler", None)
            verification_status = "unknown"
            if handler is not None:
                verification_status = getattr(handler, "last_verification_status", "unknown")
            print("[HOST] verification_status:", verification_status)

            trust_issues = trust_result.get("trust_issues")
            final_prompt = f"""
The user asked: {user_input}

You called the Jira tool '{tool_name}' with arguments:
{json.dumps(tool_args, indent=2)}

The tool returned this (may be a signed AgentDNA envelope):
{tool_output_text}

Verification status from AgentDNA: {verification_status}
Trust issues (if any): {json.dumps(trust_issues, indent=2)}

Instructions:
- If verification_status == "ok" and trust_issues is empty or null:
  - You may treat the tool output as trustworthy.
  - Answer normally and briefly mention that the result is verified.
- If verification_status == "failed" OR trust_issues is non-empty:
  - Clearly warn the user that the result is unverified or has trust issues.
  - You may still summarize what the tool reported, but label it as "unverified".
- Keep the answer in plain natural language, no JSON, no code fences.
"""
            print("[HOST] Final prompt to Gemini:\n", final_prompt[:600])
            final_response = client.models.generate_content(
                model=model_id,
                contents=final_prompt,
            )
            answer = (final_response.text or "").strip()
            print("[HOST] Final answer from Gemini:", repr(answer))

            return answer, tool_name, tool_args, tool_output_text


def run_agent_sync(user_input: str):
    return asyncio.run(run_agent_turn(user_input))


# ─────────────────────────────
# Streamlit UI
# ─────────────────────────────

st.set_page_config(page_title="Jira MCP Agent")

st.sidebar.subheader("Controls")

if "inject_fake" not in st.session_state:
    st.session_state.inject_fake = False

st.sidebar.checkbox(
    "Simulate tampering",
    key="inject_fake",
)

handler = getattr(dna, "handler", None)
if handler is not None:
    handler.inject_fake = bool(st.session_state.inject_fake)

nft_id = get_nft_token_from_host()
if not nft_id:
    st.sidebar.write("No NFT token available (dna.handler.nft_token not set)")

latest_only = False

if st.sidebar.button("History Records"):
    if not nft_id:
        st.sidebar.warning("No NFT ID found for this host agent")
    else:
        with st.spinner("Fetching NFT data…"):
            nft_resp = fetch_nft_data(nft_id, latest_only)

        def decode_nft_state(state: dict) -> dict:
            state = dict(state)  # shallow copy
            nft_data = state.get("NFTData")
            if isinstance(nft_data, str):
                try:
                    state["NFTData"] = json.loads(nft_data)
                except json.JSONDecodeError:
                    pass
            return state

        if isinstance(nft_resp, list):
            decoded = [decode_nft_state(s) for s in nft_resp]
        elif isinstance(nft_resp, dict):
            decoded = decode_nft_state(nft_resp)
        else:
            decoded = nft_resp 

        if "messages" not in st.session_state:
            st.session_state.messages = []

        pretty = json.dumps(decoded, indent=2)
        st.session_state.messages.append(
            {
                "role": "agent",
                "content": f"NFT history for {nft_id}:\n\n```json\n{pretty}\n```",
            }
        )
        st.rerun()

st.title("Jira MCP Agent")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    st.markdown(f"**{msg['role'].title()}:** {msg['content']}")

st.markdown("---")

query = st.text_area("Enter your Jira request:", height=80)

if st.button("Send") and query.strip():
    st.session_state.messages.append({"role": "user", "content": query})

    with st.spinner("Processing..."):
        answer, tool_name, tool_args, tool_output = run_agent_sync(query)

    st.session_state.messages.append({"role": "agent", "content": answer})

    st.rerun()
