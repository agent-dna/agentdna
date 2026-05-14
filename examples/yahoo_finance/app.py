import os
import sys
import json
import asyncio

import streamlit as st
from dotenv import load_dotenv, find_dotenv
from google import genai

from agentdna import AgentDNA
from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client

load_dotenv(find_dotenv())

AGENTDNA_API_KEY = os.environ.get("AGENTDNA_API_KEY")
HOST_AGENT_NAME = os.environ.get("HOST_AGENT_NAME")
MCP_TOOL_NAME = os.environ.get("MCP_TOOL_NAME")

if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")
if not HOST_AGENT_NAME:
    raise RuntimeError("Missing HOST_AGENT_NAME")
if not MCP_TOOL_NAME:
    raise RuntimeError("Missing MCP_TOOL_NAME")

dna = AgentDNA(alias=HOST_AGENT_NAME, api_key=AGENTDNA_API_KEY)

SYSTEM_PROMPT = """
You are a Yahoo Finance assistant.
Use Yahoo MCP tools when needed.

Tools:
1) get_quote(symbol: string)
2) get_history(symbol: string, period: string)

Rules:
- If a tool is needed, return only JSON: {"tool": "<name>", "args": {...}}
- If no tool is needed, return only JSON: {"answer": "<text>"}
- Do not use markdown or code fences.
"""


def init_gemini():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env")
    return genai.Client(api_key=api_key)


def extract_json(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    if "```" in raw:
        for part in raw.split("```"):
            if "{" in part and "}" in part:
                raw = part
                break

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
                contents=f"{SYSTEM_PROMPT}\nUser request: {user_input}\nReturn one JSON object",
            ).text

            decision = json.loads(extract_json(decision_raw))

            if "tool" not in decision:
                return decision.get("answer", decision_raw)

            tool_name = decision["tool"]
            tool_args = decision.get("args", {})

            # ── AgentDNA: sign the request → call MCP → verify the reply ──
            host_message = {
                "user_query": user_input,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }
            env = dna.build(host_message)

            tool_result = await session.call_tool(
                tool_name,
                arguments={**tool_args, "dna_envelope": str(env)},
            )

            tool_output_text = "\n".join(
                block.text for block in tool_result.content
                if isinstance(block, mcp_types.TextContent)
            )

            result = await dna.handle(
                tool_output_text,
                original=env,
                remote_name=MCP_TOOL_NAME,
            )

            final_prompt = f"""
User asked: {user_input}

Verified tool output:
{json.dumps(result.payload, indent=2)}

Verification status: {result.verification_status}
Trust issues: {json.dumps(result.trust_issues)}

Answer plainly.
"""

            final = client.models.generate_content(
                model=model_id,
                contents=final_prompt,
            )

            return final.text.strip()


def run_agent_sync(user_input: str):
    return asyncio.run(run_agent_turn(user_input))


# ─────────────────────────────
# Streamlit UI
# ─────────────────────────────

st.set_page_config("Yahoo Finance MCP Demo")
st.title("Yahoo Finance MCP Agent")

st.info(
    "⚠️ **Disclaimer**  \n"
    "This demo uses Yahoo Finance data accessed via automated web retrieval techniques. "
    "As Yahoo Finance does not provide an official public API for all endpoints, "
    "data availability and response consistency are best-effort and may occasionally be incomplete or unavailable."
)

st.markdown("### Example Prompts")
st.code("What is the current price of AAPL?", language="text")
st.code("Show me the last 5 days of price history for TSLA.", language="text")
st.code("Get the latest stock price for MSFT.", language="text")

st.markdown("---")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "chain_history" not in st.session_state:
    st.session_state.chain_history = []

# ── Sidebar: NFT + chain history ──
with st.sidebar:
    st.subheader("Audit Log")
    nft_id = dna.nft_token or ""
    st.caption(f"NFT: `{nft_id}`" if nft_id else "NFT: (none yet)")

    if st.button("History Records", disabled=not nft_id):
        with st.spinner("Fetching NFT data…"):
            st.session_state.chain_history = dna.history()
        st.rerun()

# Chain history — foldable JSON tree
if st.session_state.chain_history:
    with st.expander(
        f"Chain History — NFT `{nft_id}` — {len(st.session_state.chain_history)} record(s)",
        expanded=True,
    ):
        st.json(st.session_state.chain_history, expanded=2)

for msg in st.session_state.messages:
    st.markdown(f"**{msg['role'].title()}:** {msg['content']}")

query = st.text_area("Ask about a stock:")

if st.button("Send") and query.strip():
    st.session_state.messages.append({"role": "user", "content": query})
    with st.spinner("Working..."):
        answer = run_agent_sync(query)
    st.session_state.messages.append({"role": "agent", "content": answer})
    st.rerun()
