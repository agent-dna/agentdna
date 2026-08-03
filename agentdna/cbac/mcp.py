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

import json
from collections.abc import Callable

from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams, CallToolResult, TextContent

# Re-exported so MCP users import the whole recipe from one place.
from .guard import (
    _authorize,
    _default_intent,
    _report_lhi,
    cbac_context,
    cbac_guard,
    get_config,
    get_context,
)

__all__ = [
    "CBACMiddleware",
    "intent_interceptor",
    "cbac_intercept",
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


async def cbac_intercept(request, handler):
    """Client-side tool interceptor that is itself the CBAC enforcement point.

    Unlike :func:`intent_interceptor` -- which only ferries intent to a
    *cooperating* server running :class:`CBACMiddleware` + ``@cbac_guard`` --
    this authorizes each outgoing call on the client, so a third-party MCP
    server you do not own (and cannot decorate) still gets governed.

    Authorization is decision-only, so ``handler`` is the execute step: a
    non-allow decision short-circuits and the call never leaves the client;
    an allowed call is forwarded and its outcome folded into the trust
    score. Requires an open :func:`cbac_context`; without one it is a
    passthrough (governance opted out), mirroring ``@cbac_guard``.
    """
    ctx = get_context()
    if ctx is None:
        return await handler(request)

    intent = _default_intent(request.name, request.args)
    cfg = get_config()
    try:
        decision, detail, scores = await _authorize(ctx, intent, cfg)
    except Exception as exc:
        decision, detail, scores = "error", str(exc), {}

    if decision != "allow":
        # Readable denial, not a raised error -- mirrors @cbac_guard(on_deny="return")
        # so the agent loop can see the block and adapt.
        status = "denied" if decision == "deny" else "error"
        body = json.dumps({"status": status, "error": detail})
        return CallToolResult(content=[TextContent(type="text", text=body)], isError=False)

    result = await handler(request)
    output_score = 0.0 if getattr(result, "isError", False) else 1.0
    await _report_lhi(ctx, request.name, "mcp", scores, output_score, cfg)
    return result
