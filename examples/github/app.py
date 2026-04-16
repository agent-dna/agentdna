import os
import sys
import json
import asyncio
import streamlit as st
from dotenv import load_dotenv
from google import genai

from agentdna import AgentDNA, NodeClient
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

dna = AgentDNA(alias=HOST_AGENT_NAME, role="host", api_key=AGENTDNA_API_KEY)

node = NodeClient(alias=HOST_AGENT_NAME)
DEFAULT_BASE_URL = node.get_base_url()

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


def extract_json(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

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

            normalized = extract_json(decision_raw)
            
            try:
                decision = json.loads(normalized)
            except Exception as e:
                return decision_raw.strip(), None, None, ""

            if "tool" not in decision:
                answer = decision.get("answer", decision_raw)
                return answer.strip(), None, None, ""

            tool_name = decision["tool"]
            tool_args = decision.get("args", {})

            host_message = {
                "user_query": user_input,
                "tool_name": tool_name,
                "tool_args": tool_args
            }
            
            envelope = dna.build(
                original_message=json.dumps(host_message),
                state={"channel": "mcp_github"},
            )

            tool_args_with_dna = {
                **tool_args,
                "dna_envelope": envelope["host_json"],
            }
            
            # Call the respective MCP tool
            tool_result = await session.call_tool(tool_name, arguments=tool_args_with_dna)

            parts: list[str] = []
            for block in tool_result.content:
                if isinstance(block, mcp_types.TextContent):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            tool_output_text = "\n".join(parts)

            trust_result = await dna.handle(
                resp_parts=[{"text": tool_output_text}],
                original_task=json.dumps(host_message),
                remote_name=MCP_TOOL_NAME,
            )
            
            handler = getattr(dna, "handler", None)
            verification_status = "unknown"
            if handler is not None:
                verification_status = getattr(handler, "last_verification_status", "unknown")

            trust_issues = trust_result.get("trust_issues")
            final_prompt = f"""
The user asked: {user_input}

You called the Github MCP tool '{tool_name}' with arguments:
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
            final_response = client.models.generate_content(
                model=model_id,
                contents=final_prompt,
            )
            answer = (final_response.text or "").strip()
            print("[HOST] Final answer from Gemini:", repr(answer))

            return answer, tool_name, tool_args, tool_output_text

def run_agent_sync(user_input: str):
    return asyncio.run(run_agent_turn(user_input))


# Streamlit UI

st.set_page_config("GitHub MCP Demo")

handler = getattr(dna, "handler", None)

nft_id = get_nft_token_from_host()
if not nft_id:
    st.sidebar.write("No NFT token available (dna.handler.nft_token not set)")


st.title("GitHub MCP Agent")
st.markdown(f"**Repository:** [{REPO_URL}]({REPO_URL})")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    st.markdown(f"**{msg['role'].title()}:** {msg['content']}")

st.markdown("---")

query = st.text_area("Ask the Github Agent to do something:")

example_prompt = (
    'Create an issue with title "Bug: Login fails" '
    'and the description should be "Users are unable to log in using OAuth."'
)

if st.button("Send") and query.strip():
    st.session_state.messages.append({"role": "user", "content": query})

    with st.spinner("Working..."):
        answer, tool_name, tool_args, tool_output = run_agent_sync(query)
    
    st.session_state.messages.append({"role": "agent", "content": answer})

    st.rerun()

st.markdown("### Example Prompt")
st.code(example_prompt, language="text")