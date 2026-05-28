# client.py
import asyncio
import json
import os

from dotenv import load_dotenv
from agentdna import AgentDNA

from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client

load_dotenv()


def init_dna() -> AgentDNA:
    api_key = os.environ.get("AGENTDNA_API_KEY")
    if not api_key:
        raise RuntimeError("Missing AGENTDNA_API_KEY environment variable.")

    dna = AgentDNA(alias="jira_cli_host", api_key=api_key, kind="agent", enable_nft=False)
    print("✅ CLI Host DID:", dna.trust.did)
    print("✅ CLI Host base URL:", dna.trust.base_url)
    return dna


async def main() -> None:
    # Forward env vars to the MCP subprocess
    server_env = dict(os.environ)

    print("JIRA_BASE_URL:", server_env.get("JIRA_BASE_URL"))
    print("JIRA_EMAIL:", server_env.get("JIRA_EMAIL"))
    print("JIRA_API_TOKEN set?:", bool(server_env.get("JIRA_API_TOKEN")))

    server_params = StdioServerParameters(
        command="mcp",
        args=["run", "server.py", "--transport", "stdio"],
        env=server_env,
    )

    dna = init_dna()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])

            # Example JQL: issues assigned to you
            jql = "assignee = currentUser() ORDER BY created DESC"

            host_msg = {
                "tool": "search_issues",
                "jql": jql,
                "max_results": 5,
            }
            built = dna.build(
                original_message=json.dumps(host_msg),
                state={"channel": "mcp_jira_cli"},
            )
            dna_envelope = built["host_json"]

            result = await session.call_tool(
                "search_issues",
                arguments={
                    "jql": jql,
                    "max_results": 5,
                    "dna_envelope": dna_envelope,
                },
            )

            parts: list[str] = []
            if result.content:
                for block in result.content:
                    if isinstance(block, mcp_types.TextContent):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))

            tool_output_text = "\n".join(parts) if parts else ""
            print("\nsearch_issues (raw):")
            print(tool_output_text or "No issues found for that JQL.")

            trust = await dna.handle(
                resp_parts=[{"text": tool_output_text}],
                original_task=json.dumps(host_msg),
                remote_name="jira_server",
                execute_nft=False,
            )

            handler = getattr(dna, "handler", None)
            verification_status = "unknown"
            if handler is not None:
                verification_status = getattr(handler, "last_verification_status", "unknown")

            print("\n🔐 Trust summary")
            print("verification_status:", verification_status)
            print("trust_issues:", json.dumps(trust.get("trust_issues"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())