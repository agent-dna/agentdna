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
HOST_AGENT_NAME = os.environ.get("HOST_AGENT_NAME")
MCP_TOOL_NAME = os.environ.get("MCP_TOOL_NAME")

if not AGENTDNA_API_KEY:
    raise RuntimeError("Missing AGENTDNA_API_KEY")
if not HOST_AGENT_NAME:
    raise RuntimeError("Missing HOST_AGENT_NAME")
if not MCP_TOOL_NAME:
    raise RuntimeError("Missing MCP_TOOL_NAME")

dna = AgentDNA(alias=HOST_AGENT_NAME, role="host", api_key=AGENTDNA_API_KEY)

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

            host_message = {
                "user_query": user_input,
                "tool_name": tool_name,
                "tool_args": tool_args,
            }

            envelope = dna.build(
                original_message=json.dumps(host_message),
                state={"channel": "mcp_yahoo"},
            )

            tool_args_with_dna = {
                **tool_args,
                "dna_envelope": envelope["host_json"],
            }

            # ───────── sidebar-controlled tampering ─────────
            if st.session_state.get("inject_fake", False):
                tool_args_with_dna["inject_fake"] = True

            tool_result = await session.call_tool(
                tool_name,
                arguments=tool_args_with_dna,
            )

            parts = []
            for block in tool_result.content:
                if isinstance(block, mcp_types.TextContent):
                    parts.append(block.text)

            tool_output_text = "\n".join(parts)

            trust = await dna.handle(
                resp_parts=[{"text": tool_output_text}],
                original_task=json.dumps(host_message),
                remote_name=MCP_TOOL_NAME,
            )

            verification_status = getattr(
                getattr(dna, "handler", None),
                "last_verification_status",
                "unknown",
            )

            final_prompt = f"""
User asked: {user_input}

Tool output:
{tool_output_text}

Verification status: {verification_status}
Trust issues: {json.dumps(trust.get("trust_issues"))}

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

# ───────── Main UI ─────────

st.title("Yahoo Finance MCP Agent")

# Disclaimer
st.info(
    "⚠️ **Disclaimer**  \n"
    "This demo uses Yahoo Finance data accessed via automated web retrieval techniques. "
    "As Yahoo Finance does not provide an official public API for all endpoints, "
    "data availability and response consistency are best-effort and may occasionally be incomplete or unavailable."
)

# Example prompts
st.markdown("### Example Prompts")
st.code("What is the current price of AAPL?", language="text")
st.code("Show me the last 5 days of price history for TSLA.", language="text")
st.code("Get the latest stock price for MSFT.", language="text")

st.markdown("---")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    st.markdown(f"**{msg['role'].title()}:** {msg['content']}")

query = st.text_area("Ask about a stock:")

if st.button("Send") and query.strip():
    st.session_state.messages.append({"role": "user", "content": query})
    with st.spinner("Working..."):
        answer = run_agent_sync(query)
    st.session_state.messages.append({"role": "agent", "content": answer})
    st.rerun()
