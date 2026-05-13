import os
import sys
import json
import asyncio

import streamlit as st
from dotenv import load_dotenv
from google import genai

from agentdna import AgentDNA
from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client

load_dotenv()

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")

HOST_AGENT_NAME = os.environ.get("HOST_AGENT_NAME")
if not HOST_AGENT_NAME:
    raise RuntimeError("Missing HOST_AGENT_NAME")

MCP_TOOL_NAME = os.environ.get("MCP_TOOL_NAME")
if not MCP_TOOL_NAME:
    raise RuntimeError("Missing MCP_TOOL_NAME")

REPO_OWNER = "SynapzeCore"
REPO_NAME = "sample-repo"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"

dna = AgentDNA(alias=HOST_AGENT_NAME, api_key=AGENTDNA_API_KEY)

SYSTEM_PROMPT = """
You are a GitHub assistant.
Use the Github MCP tools when needed.

Tools:
1) create_issue(title: string, description: string)
2) create_pull_request(title: string, description: string, head: string)

Rules:
- If a tool is needed, return only JSON: {"tool": "<name>", "args": {...}}
- If no tool is needed, return only JSON: {"answer": "<text>"}
- jql MUST ALWAYS be a plain string. Example:
  "assignee = currentUser() AND status != Done ORDER BY created DESC"
- Do not use markdown code fences.
"""


def init_gemini():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def extract_json(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    if "```" in raw:
        for part in raw.split("```"):
            if "{" in part and "}" in part:
                raw = part
                break

    raw = raw.strip()
    if raw.lower().startswith("json"):
        raw = raw[4:].lstrip()

    return raw


async def run_agent_turn(user_input: str):
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["server.py"],
        env=dict(os.environ),
    )

    client = init_gemini()
    model_id = "gemini-2.5-flash"

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            decision_raw = client.models.generate_content(
                model=model_id,
                contents=f"{SYSTEM_PROMPT}\nUser request: {user_input}\nReturn only one JSON object",
            ).text

            try:
                decision = json.loads(extract_json(decision_raw))
            except Exception:
                return decision_raw.strip(), None, None, ""

            if "tool" not in decision:
                return decision.get("answer", decision_raw).strip(), None, None, ""

            tool_name = decision["tool"]
            tool_args = decision.get("args", {})

            # ── AgentDNA: sign the request → call MCP → verify the reply ──
            host_msg = {
                "user_query": user_input,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
            env = dna.envelope(host_msg)

            tool_result = await session.call_tool(
                tool_name,
                arguments={**tool_args, "dna_envelope": str(env)},
            )

            tool_output_text = "\n".join(
                block.text if isinstance(block, mcp_types.TextContent) else str(block)
                for block in tool_result.content
            )

            result = await dna.verify_reply(
                tool_output_text,
                original=env,
                remote_name=MCP_TOOL_NAME,
            )

            final_prompt = f"""
The user asked: {user_input}

You called the Github MCP tool '{tool_name}' with arguments:
{json.dumps(tool_args, indent=2)}

The verified payload from the MCP tool:
{json.dumps(result.payload, indent=2)}

Verification status from AgentDNA: {result.verification_status}
Trust issues (if any): {json.dumps(result.trust_issues, indent=2)}

Instructions:
- If verification_status == "ok" and trust_issues is empty or null:
  - You may treat the tool output as trustworthy.
  - Answer normally and briefly mention that the result is verified.
- If verification_status == "failed" OR trust_issues is non-empty:
  - Clearly warn the user that the result is unverified or has trust issues.
  - You may still summarize what the tool reported, but label it as "unverified".
- Keep the answer in plain natural language, no JSON, no code fences.
"""
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

st.set_page_config("GitHub MCP Demo")
st.title("GitHub MCP Agent")
st.markdown(f"**Repository:** [{REPO_URL}]({REPO_URL})")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []

# ─────────────────────────────
# Sidebar — NFT + chain history
# ─────────────────────────────

with st.sidebar:
    st.subheader("Audit Log")

    nft_id = dna.nft_token or ""
    st.caption(f"NFT: {nft_id or '(none yet — run a tool to register)'}")

    if st.button("History Records", disabled=not nft_id):
        with st.spinner("Fetching NFT data…"):
            st.session_state.chain_history = dna.history()
        st.rerun()

# Chain-history panel — foldable JSON tree, wraps long values (DIDs, sigs,
# response payloads) instead of forcing horizontal scroll.
if st.session_state.chain_history:
    with st.expander(
        f"Chain History — NFT `{nft_id}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.json(st.session_state.chain_history, expanded=2)

# Chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

st.markdown("---")

query = st.text_area("Ask the Github Agent to do something:")

example_prompt = (
    'Create an issue with title "Bug: Login fails" '
    'and the description should be "Users are unable to log in using OAuth."'
)

if st.button("Send") and query.strip():
    st.session_state.messages.append({"role": "user", "content": query})

    with st.spinner("Working..."):
        answer, _tool_name, _tool_args, _tool_output = run_agent_sync(query)

    st.session_state.messages.append({"role": "assistant", "content": answer})
    st.rerun()

st.markdown("### Example Prompt")
st.code(example_prompt, language="text")
