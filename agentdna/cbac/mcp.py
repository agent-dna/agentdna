"""
MCP integration for the CBAC guard (optional).

Install with ``pip install agent-dna[mcp]``.

``@cbac_guard`` authorizes a tool against the ambient
:func:`~agentdna.cbac.guard.cbac_context` -- trivial when the tool runs in the
same process. Over MCP the tool runs in the server process, and a
contextvar cannot cross a process boundary. This module carries the one
value that must cross -- the root user intent -- as a hidden tool
argument, so guarded tools served over MCP get authorized exactly like
in-process ones and the ``@cbac_guard`` decorators need no change:

  - client side: :func:`intent_interceptor` injects the intent into every
    outgoing call, *after* the framework built the LLM-facing schema, so
    the model never sees or controls it.
  - server side: :class:`CBACMiddleware` pops it back off before argument
    validation and re-opens a ``cbac_context`` so the guard has an agent id
    + intent to authorize against.

Typical wiring (one import surface)::

    from agentdna.cbac.mcp import cbac_guard, cbac_context, CBACMiddleware, intent_interceptor

    # server
    mcp.add_middleware(CBACMiddleware(agent_id_provider=my_agent_id))

    # client
    client = MultiServerMCPClient(servers, tool_interceptors=[intent_interceptor])

    # request entry point
    with cbac_context(agent_id=my_agent_id, user_intent=root_intent):
        await agent.ainvoke(...)
"""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams

# Re-exported so MCP users import the whole recipe from one place.
from .guard import cbac_context, cbac_guard, get_context

__all__ = [
    "CBACMiddleware",
    "intent_interceptor",
    "INTENT_ARG",
    "cbac_guard",
    "cbac_context",
]

# The hidden tool argument carrying the root user intent across the wire.
# It is never part of a tool's declared schema, so the LLM never sees it;
# the middleware strips it before the tool's own arguments are validated.
INTENT_ARG = "user_intent"


# XXX: Not agnostic to MCP-SDK
class CBACMiddleware(Middleware):
    """Server-side: restore the governance context from the hidden
    ``user_intent`` argument so ``@cbac_guard`` can authorize the call.

    ``agent_id_provider`` returns the server's own agent id (whose
    on-chain policy is checked). Returning ``None`` leaves governance off
    for that call -- the tool runs unguarded -- so keep it reliable.
    """

    def __init__(self, agent_id_provider: Callable[[], str | None]):
        self._agent_id_provider = agent_id_provider

    async def on_call_tool(self, context: MiddlewareContext[CallToolRequestParams], call_next):
        args = dict(context.message.arguments or {})
        user_intent = args.pop(INTENT_ARG, "")
        context = context.copy(message=context.message.model_copy(update={"arguments": args}))

        agent_id = self._agent_id_provider()
        if not agent_id:
            return await call_next(context)

        with cbac_context(agent_id=agent_id, user_intent=user_intent or ""):
            return await call_next(context)


async def intent_interceptor(request, handler):
    """Client-side tool interceptor: inject the ambient root user intent
    into every outgoing call, hidden from the LLM (added after the
    framework's arg parsing). Pass ``tool_interceptors=[intent_interceptor]``
    when building the MCP client.
    """
    ctx = get_context()
    if ctx is None:
        return await handler(request)
    return await handler(request.override(args={**request.args, INTENT_ARG: ctx.user_intent}))
