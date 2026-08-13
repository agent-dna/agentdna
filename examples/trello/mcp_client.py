from config import settings

APP = "Trello"

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection


async def load_tools():
    if not settings.trello_mcp_url:
        raise ValueError("TRELLO_MCP_URL is not configured")

    client = MultiServerMCPClient(
        {
            "trello": StreamableHttpConnection(
                transport="streamable_http",
                url=settings.trello_mcp_url,
            )
        }
    )

    tools = await client.get_tools()
    return tools
