"""Server authentication observation point.

Runs in the MCP server middleware and records how each request authenticated.
It observes from outside the agent, so the agent cannot shape the evidence.

Disabled by default. See `agentdna.auth.sink` for how to turn it on.

This module:

  * reads only the headers in `AUTH_HEADERS`
  * never validates credentials
  * never makes access decisions
  * never breaks a request when evidence collection fails

MCP middleware runs above the HTTP layer, so the headers are reached through
the server's request context where one exists. Servers on the official MCP SDK
have no such context - wrap the ASGI app with `capture_headers()` to make the
authentication headers available to the middleware.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

from agentdna.auth import sink
from agentdna.auth.evidence import (
    AUTH_HEADERS,
    STATUS_UNKNOWN,
    AuthEvidence,
    observe,
)
from agentdna.auth.key import load_key

# Two legs, two sources. The server receives a request and then calls its own
# backend to answer it - and the credential on the way out is usually not the
# one that came in. Both legs share a request_id, so without separate sources
# they would collide on (request_id, source) and one would overwrite the other.
SOURCE_SERVER_IN = "server_in"
SOURCE_SERVER_OUT = "server_out"

_captured: ContextVar[dict | None] = ContextVar("agentdna_request_headers", default=None)


def record_request(dna, workflow, context) -> None:
    """Observe and write authentication evidence for this request.

    This is the inbound leg only: what the agent presented to this server. What
    the server then presents to its own backend is the outbound leg, recorded
    as `server_out` by the client-side observation point running in this same
    process. Identity usually stops on that second leg, not this one.

    auth_status is `unknown`, deliberately. This point runs where the request
    arrived, not where the credential was judged, so whether it was accepted is
    something we have not observed. A point in front of the server sees that.
    """
    if not sink.enabled():
        return

    try:
        envelope = workflow.get_latest_envelope()
        facts = observe(request_headers(context), load_key(dna.config_dir, dna.logger))

        evidence = AuthEvidence.from_facts(
            facts,
            run_id=envelope.run_id,
            request_id=envelope.signature,
            source=SOURCE_SERVER_IN,
            destination=dna.get_actor_id(),
            auth_status=STATUS_UNKNOWN,
            observed_at=time.time(),
        )

        sink.send(evidence, dna)
    except Exception as exc:
        dna.logger.warning("agentdna.authevidence.record_failed", error=str(exc))


# --- reaching the headers -------------------------------------------------
#
# Two supported servers, one source each. Both return whatever they can see;
# request_headers() owns the guarantee that only auth headers come back.


def request_headers(context) -> dict:
    """The authentication headers visible at this observation point.

    Empty when neither source can answer, which reads as "no credential was
    presented". That is the truth over stdio, and an honest gap elsewhere - it
    is never an error, because evidence must not break a request.
    """
    return _only_auth_headers(_fastmcp_headers() or _captured_headers())


def _fastmcp_headers() -> dict:
    """FastMCP keeps the live request in a context variable.

    `include` is not optional: without it the accessor strips `authorization`
    and returns every header except the one this is for.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        return dict(get_http_headers(include=set(AUTH_HEADERS)))
    except Exception:
        return {}


def _captured_headers() -> dict:
    """What capture_headers() stashed on the way in."""
    return dict(_captured.get() or {})


def _only_auth_headers(headers: dict) -> dict:
    """The guarantee: nothing but the headers we classify leaves this module."""
    wanted = set(AUTH_HEADERS)
    return {
        str(name).lower(): value for name, value in headers.items() if str(name).lower() in wanted
    }


def capture_headers(app):
    """Expose the authentication headers to protocol-level middleware.

    Needed by servers on the official MCP SDK, which carries no request context
    of its own - its ServerRequestContext.request is the MCP request, not the
    HTTP one. FastMCP servers do not need this.

        app = capture_headers(mcp_app)

    Filters as it captures, so no other header is ever held. The request is not
    altered and passes through untouched.
    """

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)

        headers = {}
        for raw_name, raw_value in scope.get("headers") or []:
            try:
                headers[raw_name.decode("latin-1")] = raw_value.decode("latin-1")
            except Exception:
                continue

        token = _captured.set(_only_auth_headers(headers))
        try:
            return await app(scope, receive, send)
        finally:
            _captured.reset(token)

    return wrapped
