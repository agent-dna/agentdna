from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from crewai.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection
from pydantic import BaseModel, Field, PrivateAttr


class DescribeTableInput(BaseModel):
    table: str = Field(description="Allowed table name returned by sqlite_list_tables.")


class QueryInput(BaseModel):
    query: str = Field(description="One read-only SELECT statement.")


class AdapterTool(BaseTool):
    """CrewAI tool facade backed by a LangChain MCP adapter tool."""

    _adapter_tool: Any = PrivateAttr()

    def __init__(self, adapter_tool: Any, **data: Any) -> None:
        super().__init__(**data)
        self._adapter_tool = adapter_tool

    def _run(self, **kwargs: Any) -> str:
        return str(asyncio.run(self._adapter_tool.ainvoke(kwargs)))


def build_client() -> MultiServerMCPClient:
    environment = dict(os.environ)
    examples_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [examples_root, environment.get("PYTHONPATH")]))
    return MultiServerMCPClient(
        {
            "sqlite": StdioConnection(
                transport="stdio",
                command=sys.executable,
                args=["-m", "sqlite_analytics_agent.mcp_server"],
                env=environment,
            )
        }
    )


async def load_tools() -> list[BaseTool]:
    discovered = {tool.name: tool for tool in await build_client().get_tools()}
    required = {"sqlite_list_tables", "sqlite_describe_table", "sqlite_query"}
    missing = required.difference(discovered)
    if missing:
        raise RuntimeError(f"SQLite MCP server did not expose expected tools: {sorted(missing)}")
    return [
        AdapterTool(adapter_tool=discovered["sqlite_list_tables"], name="sqlite_list_tables", description="Discover database tables through MCP."),
        AdapterTool(adapter_tool=discovered["sqlite_describe_table"], name="sqlite_describe_table", description="Inspect columns and keys for an allowed table.", args_schema=DescribeTableInput),
        AdapterTool(adapter_tool=discovered["sqlite_query"], name="sqlite_query", description="Run one validated SELECT query through MCP.", args_schema=QueryInput),
    ]