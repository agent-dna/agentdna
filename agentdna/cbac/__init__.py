"""CBAC governance layer: the framework-agnostic guard + optional MCP glue.

The guard API is re-exported here for ``from agentdna.cbac import ...``.
The MCP integration lives in :mod:`agentdna.cbac.mcp` and is imported
separately (it needs the ``[mcp]`` extra), so importing this package does
not pull in fastmcp.
"""

from .guard import (
    GovernanceContext,
    GuardConfig,
    cbac_context,
    cbac_guard,
    configure,
    get_config,
    get_context,
)

__all__ = [
    "GovernanceContext",
    "GuardConfig",
    "cbac_context",
    "cbac_guard",
    "configure",
    "get_config",
    "get_context",
]
