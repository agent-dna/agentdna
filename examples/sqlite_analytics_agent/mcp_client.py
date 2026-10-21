from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from crewai.tools import BaseTool
from crewai_tools import MCPServerAdapter

from config import settings

REQUIRED_TOOLS = ("sqlite_list_tables", "sqlite_describe_table", "sqlite_query")


@contextmanager
def sqlite_tools(mcp_url: str) -> Iterator[list[BaseTool]]:
    """
    Discover the SQLite capabilities through CrewAI's native MCP client.

    `mcp_url` is an AgentDNA Provenance Relay session URL, which signs the tool calls;
    the client itself carries no AgentDNA patch. The connection is closed when
    the block ends.
    """

    with MCPServerAdapter(
        {
            "url": mcp_url,
            "transport": "streamable-http",
            "headers": settings.sqlite_mcp_headers,
        },
        connect_timeout=settings.mcp_timeout_seconds,
    ) as tools:
        discovered = {tool.name: tool for tool in tools}

        missing = set(REQUIRED_TOOLS).difference(discovered)
        if missing:
            raise RuntimeError(
                "SQLite MCP server did not expose expected tools: "
                f"{sorted(missing)}"
            )

        yield [discovered[name] for name in REQUIRED_TOOLS]
