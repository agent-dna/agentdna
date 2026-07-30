"""
MCP client adapter — connects to the GitHub MCP server and exposes its
tools as LangChain Tool objects the Worker LLM can call.
"""

from __future__ import annotations


from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import settings


async def get_github_tools():
    """
    Discover tools served by the GitHub MCP server.
    Returns a list of LangChain-compatible Tool objects.
    """
    # A tool call (create_issue/create_pr) blocks on the MCP server's CBAC check,
    # which runs a local model for drift scoring and can take minutes. Raise the
    # client timeouts to match the CBAC timeout so the worker→MCP call doesn't
    # give up first. ``sse_read_timeout`` (default ~5 min) is the one that
    # governs how long we wait for the tool result.
    timeout_s = float("103600")
    client = MultiServerMCPClient(
        {
            "github": {
                "url": settings.mcp_server_url,
                "transport": "streamable_http",
                "timeout": timeout_s,
                "sse_read_timeout": timeout_s,
            }
        }
    )
    return await client.get_tools()
