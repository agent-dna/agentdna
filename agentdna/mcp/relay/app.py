"""The relay's HTTP surface: one MCP endpoint per run and server.
    /runs/{run_id}/mcp/{server}   Streamable HTTP, forwarded to the named MCP server
Runs themselves are opened and closed by calling `app.state.service`
directly, so there is no endpoint for them.
The MCP endpoint is a pass-through that understands just enough of the
protocol to find tools/call requests and their responses, in either a JSON or
an SSE response body. Everything else - initialisation, listing, notifications,
the GET stream, session headers - reaches the upstream server untouched.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agentdna.log import get_logger

from .config import ProvenanceRelayConfig
from .propagation import (
    PendingCall,
    ToolCallRejected,
    abandon_tool_call,
    begin_tool_call,
    finish_tool_call,
    is_response_to,
    is_tool_call,
    jsonrpc_error,
)
from .runs import Run, RunRegistry
from .service import RunService

logger = get_logger("agentdna.relay")

# Hop-by-hop headers, plus the ones the relay recomputes. accept-encoding is
# dropped so the upstream answers uncompressed and SSE can be read line by line.
_REQUEST_HEADERS_DROPPED = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-connection",
    "accept-encoding",
}
_RESPONSE_HEADERS_DROPPED = {
    "content-length",
    "content-encoding",
    "connection",
    "keep-alive",
    "transfer-encoding",
}


def create_app(
    config: ProvenanceRelayConfig,
    upstream: httpx.AsyncClient | None = None,
) -> Starlette:
    """Build the relay application.
    Runs are opened and closed through `app.state.service`; tool calls in a
    run are signed as the agent that opened it. `upstream` is for tests; by
    default the relay owns its own client.
    """

    runs = RunRegistry(ttl_seconds=config.run_ttl_seconds)
    service = RunService(config, runs)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # For callers on other threads: run operations execute on this loop.
        app.state.loop = asyncio.get_running_loop()
        if upstream is not None:
            yield  # attached below, and owned by whoever passed it in
            return
        async with httpx.AsyncClient() as client:
            app.state.upstream = client
            yield

    # -- MCP endpoint -------------------------------------------------------

    async def mcp(request: Request) -> Response:
        run = runs.get(request.path_params["run_id"])
        if run is None:
            return _problem(404, "unknown or expired run")

        upstream_url = config.upstreams.get(request.path_params["server"])
        if upstream_url is None:
            return _problem(404, f"unknown MCP server {request.path_params['server']!r}")

        body = await request.body()
        pending: PendingCall | None = None

        if request.method == "POST":
            message = _parse_json(body)

            if isinstance(message, list) and any(is_tool_call(m) for m in message):
                return JSONResponse(
                    jsonrpc_error(None, "AgentDNA relay: batched tools/call is not supported"),
                    status_code=400,
                )

            if is_tool_call(message):
                assert isinstance(message, dict)
                try:
                    pending = await begin_tool_call(run, message)
                except ToolCallRejected as exc:
                    return JSONResponse(
                        jsonrpc_error(message["id"], f"AgentDNA relay: {exc}"),
                        # A call refused because the run ended is a well-formed
                        # request: answer it as a JSON-RPC error, which MCP clients
                        # report as a refused call, not as a broken connection.
                        status_code=200 if run.ended is not None else 400,
                    )
                except Exception as exc:
                    logger.error("relay.tool_call.build_failed", run_id=run.run_id, error=str(exc))
                    return JSONResponse(
                        jsonrpc_error(
                            message["id"], f"AgentDNA relay: unable to sign the request: {exc}"
                        )
                    )
                body = json.dumps(message, separators=(",", ":")).encode("utf-8")

        client: httpx.AsyncClient = request.app.state.upstream
        upstream_request = client.build_request(
            request.method,
            _upstream_url(upstream_url, request),
            headers=_forward_headers(request),
            content=body if body else None,
            timeout=_timeout(config, request.method),
        )

        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("relay.upstream.unreachable", run_id=run.run_id, error=str(exc))
            if pending is None:
                return _problem(502, f"upstream MCP server unreachable: {exc}")
            await abandon_tool_call(pending)
            return JSONResponse(
                jsonrpc_error(
                    pending.request_id, f"AgentDNA relay: upstream MCP server unreachable: {exc}"
                ),
                status_code=502,
            )

        if pending is None:
            return _stream_through(upstream_response)

        return await _tool_call_response(run, pending, upstream_response)

    routes = [
        Route("/runs/{run_id}/mcp/{server}", mcp, methods=["GET", "POST", "DELETE"]),
        Route("/runs/{run_id}/mcp/{server}/", mcp, methods=["GET", "POST", "DELETE"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.runs = runs
    app.state.service = service
    if upstream is not None:
        app.state.upstream = upstream
    return app


# -- tools/call responses ------------------------------------------------------


async def _tool_call_response(
    run: Run,
    pending: PendingCall,
    upstream_response: httpx.Response,
) -> Response:
    content_type = upstream_response.headers.get("content-type", "").lower()
    headers = _response_headers(upstream_response)

    if upstream_response.status_code >= 400 or not (
        content_type.startswith("application/json") or content_type.startswith("text/event-stream")
    ):
        # Not an answer to the call; whatever it is, the agent sees it as is.
        await abandon_tool_call(pending)
        return _stream_through(upstream_response)

    if content_type.startswith("application/json"):
        try:
            raw = await upstream_response.aread()
        except httpx.HTTPError as exc:
            await abandon_tool_call(pending)
            return JSONResponse(
                jsonrpc_error(
                    pending.request_id,
                    f"AgentDNA relay: upstream MCP server failed mid-response: {exc}",
                ),
                status_code=502,
            )
        finally:
            await upstream_response.aclose()

        message = _parse_json(raw)
        if not is_response_to(message, pending.request_id):
            await abandon_tool_call(pending)
            return Response(raw, status_code=upstream_response.status_code, headers=headers)

        assert isinstance(message, dict)
        answer = await finish_tool_call(pending, message)
        return Response(
            json.dumps(answer, separators=(",", ":")),
            status_code=upstream_response.status_code,
            headers=headers,
        )

    return StreamingResponse(
        _rewrite_sse(pending, upstream_response),
        status_code=upstream_response.status_code,
        headers=headers,
    )


async def _rewrite_sse(pending: PendingCall, upstream_response: httpx.Response):
    """Forward the SSE stream event by event, rewriting only the call's response.
    Progress notifications and anything else on the stream pass through as
    they arrive.
    """

    try:
        lines: list[str] = []
        async for line in upstream_response.aiter_lines():
            if line:
                lines.append(line)
                continue
            if lines:
                yield await _sse_event(pending, lines)
                lines = []
        if lines:
            yield await _sse_event(pending, lines)
    except httpx.HTTPError as exc:
        logger.warning("relay.upstream.stream_failed", run_id=pending.run.run_id, error=str(exc))
    finally:
        await upstream_response.aclose()
        # The client may have gone away; resolve the call regardless.
        with anyio.CancelScope(shield=True):
            await abandon_tool_call(pending)


async def _sse_event(pending: PendingCall, lines: list[str]) -> str:
    data_lines = [line for line in lines if line.startswith("data:")]
    data = "\n".join(_field_value(line) for line in data_lines)

    message = _parse_json(data) if data_lines and not pending.resolved else None
    if not is_response_to(message, pending.request_id):
        return "\n".join(lines) + "\n\n"

    assert isinstance(message, dict)
    answer = await finish_tool_call(pending, message)
    kept = [line for line in lines if not line.startswith("data:")]
    kept.append("data: " + json.dumps(answer, separators=(",", ":")))
    return "\n".join(kept) + "\n\n"


def _field_value(line: str) -> str:
    value = line[len("data:") :]
    return value[1:] if value.startswith(" ") else value


# -- helpers -----------------------------------------------------------------


def _stream_through(upstream_response: httpx.Response) -> StreamingResponse:
    return StreamingResponse(
        upstream_response.aiter_bytes(),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response),
        background=BackgroundTask(upstream_response.aclose),
    )


def _forward_headers(request: Request) -> list[tuple[str, str]]:
    return [
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in _REQUEST_HEADERS_DROPPED
    ]


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _RESPONSE_HEADERS_DROPPED
    }


def _upstream_url(base: str, request: Request) -> str:
    query = request.url.query
    return f"{base}?{query}" if query else base


def _timeout(config: ProvenanceRelayConfig, method: str) -> httpx.Timeout:
    # A GET is the server's long-lived notification stream: it may sit idle.
    read = None if method == "GET" else config.upstream_timeout_seconds
    return httpx.Timeout(config.upstream_timeout_seconds, connect=10.0, read=read)


def _parse_json(raw: bytes | str) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _problem(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)
