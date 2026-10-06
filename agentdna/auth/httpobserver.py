"""Records the credential on outbound HTTP calls made while serving a request.

An MCP server answers a request by calling its own backend, and the credential
on that call is usually not the one that came in. That second leg is where
identity normally stops, so without it the run reports "identity reached this
server" and says nothing about where it went.

The hook is installed on every httpx client in the process, and is inert
outside an AgentDNA workflow: no workflow in context, nothing read. Inside one,
it reads the headers in AUTH_HEADERS and nothing else.

Covers httpx and requests, which between them are what agent code uses: httpx
for the protocol and model layer (MCP, CrewAI, OpenAI), requests for tool
integrations and older SDKs.

AgentDNA's own calls are never recorded. They are bookkeeping, not the run's
work. The server middleware wraps its own work - checks, CBAC, signing,
recording - in `not_observed()`, and only the tool itself in `observed()`.
The evidence post is skipped by thread: recording it would post again, and
that does not terminate.

On the client side only calls that carry a workflow are recorded. One MCP tool
call is several HTTP requests - a handshake, a stream, a teardown - and only
the call itself carries one. A plain REST call carries none either, so it is
not recorded from the client side at all: deliberate while REST has no
correlation mechanism of its own, not an oversight.

Not covered: aiohttp, and anything that is not HTTP. A tool talking to Postgres
or SQLite has no auth header to read - its credential lives in a connection
string set once at startup. Coverage here is "the backends that are HTTP", not
"the backends".
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

import httpx

from agentdna.auth import sink
from agentdna.auth.evidence import (
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    AuthEvidence,
    observe,
)
from agentdna.auth.key import load_key
from agentdna.mcp.metadata import (
    AGENTDNA_INTENT_WORKFLOW_META_KEY,
    AGENTDNA_META_KEY,
)

SOURCE_SERVER_OUT = "server_out"
SOURCE_CLIENT_OUT = "client_out"

# Bodies above this are not parsed. Nothing AgentDNA puts in `_meta` comes
# close, and an observer must not spend a request's time on a payload.
MAX_BODY_BYTES = 1_000_000

_installed = False

# The AgentDNA instance, remembered for threads that cannot see the context.
#
# MCP clients run tool calls on a thread of their own, and a ContextVar set on
# the thread that started the run is not visible there. The instance is not
# per-run - only config, a logger and an api key - so remembering the last one
# is safe where borrowing a workflow would not be.
_remembered_dna = None


def remember_dna(dna) -> None:
    """Called from a thread that can see the context, used from one that cannot."""
    global _remembered_dna
    _remembered_dna = dna


# False while AgentDNA does its own work. A ContextVar, so it follows the work
# into asyncio.to_thread - which is where CBAC posts from.
_observing: ContextVar[bool] = ContextVar("agentdna_observing", default=True)


@contextmanager
def not_observed():
    """AgentDNA's own work: calls made in here are not the run's hops."""
    token = _observing.set(False)
    try:
        yield
    finally:
        _observing.reset(token)


@contextmanager
def observed():
    """The tool's own work, inside a not_observed() block: record it."""
    token = _observing.set(True)
    try:
        yield
    finally:
        _observing.reset(token)


def install(dna=None, source: str = SOURCE_SERVER_OUT) -> None:
    """Start recording outbound calls. Safe to call more than once.

    `dna` is optional. The client side installs at import, before a run exists
    and before there is an instance to hand over; by the time anything is
    recorded the workflow in context has one.
    """
    global _installed
    if _installed or not sink.enabled():
        return
    _installed = True

    _patch_httpx(httpx.Client, dna, source, is_async=False)
    _patch_httpx(httpx.AsyncClient, dna, source, is_async=True)
    _patch_requests(dna, source)


def _patch_httpx(client_class, dna, source, is_async):
    """Append our hook to every client this class builds.

    A "response" event hook, not a "request" one. Both see the credential -
    httpx applies `auth` inside send(), so only a hook on send() itself would
    miss it - but the response is what carries the destination's answer, and
    that answer is the difference between "a credential was presented" and
    "a credential was refused".
    """
    original_init = client_class.__init__

    def patched_init(self, *args, **kwargs):
        hooks = dict(kwargs.pop("event_hooks", None) or {})
        listeners = list(hooks.get("response", []))
        listeners.append(_make_hook(dna, source, is_async))
        hooks["response"] = listeners
        kwargs["event_hooks"] = hooks
        original_init(self, *args, **kwargs)

    client_class.__init__ = patched_init


def _make_hook(dna, source, is_async):
    def observed(response):
        """`response.request` is the request as it was actually sent."""
        request = response.request
        return (
            request.headers,
            request.url.host,
            _httpx_body(request),
            response.status_code,
        )

    def record(response):
        _record(dna, source, *observed(response))

    if not is_async:
        return record

    async def record_async(response):
        _record(dna, source, *observed(response))

    return record_async


def _httpx_body(request):
    """The body, or None when it is streaming and not in memory.

    Read here rather than in `_record`, because arguments are evaluated before
    the callee's try block is entered.
    """
    try:
        return request.content
    except Exception:
        return None


def _patch_requests(dna, source):
    """Record calls made with `requests`.

    HTTPAdapter.send is the seam: requests applies auth while preparing the
    request, so by the time the adapter sees it the headers are complete - the
    same reason httpx needs its request event rather than send().

    Not urllib3, which sits underneath requests: hooking both would record
    every requests call twice.
    """
    try:
        from requests.adapters import HTTPAdapter
    except ImportError:
        return

    original_send = HTTPAdapter.send

    def patched_send(self, request, *args, **kwargs):
        status = None
        try:
            response = original_send(self, request, *args, **kwargs)
            status = response.status_code
            return response
        finally:
            # In `finally`, so a call that never reached the destination is
            # still recorded - with no verdict, because none was given.
            _record(
                dna,
                source,
                request.headers,
                urlparse(request.url).hostname or "",
                request.body,
                status,
            )

    HTTPAdapter.send = patched_send


def _record(dna, source, headers, host, body=None, status=None) -> None:
    """One outbound call. Never raises: evidence must not break a request."""
    if sink.is_sender_thread():
        return  # AgentDNA posting evidence - recording it would post again
    if not _observing.get():
        return  # AgentDNA's own checks and signing, not a hop

    dna = dna if dna is not None else _current_dna() or _remembered_dna
    if dna is None:
        return  # installed without one, and no run in context to borrow from

    try:
        # The call's own workflow, when it carried one. Read off the wire, so
        # it works on threads where the context is invisible.
        wire = _envelope_from_body(body)

        if wire is None and source == SOURCE_CLIENT_OUT:
            # The hop being served is the wrong anchor for a call the client
            # made: the agent's own transport traffic would land on it, four
            # rows per tool call, against a request none of them are.
            return

        if wire is not None:
            request_id, run_id = wire.get("signature", ""), wire.get("run_id", "") or ""
        else:
            envelope = _current_envelope()
            if envelope is None:
                return  # not serving a request, so nothing to attach this to
            request_id, run_id = envelope.signature, envelope.run_id

        facts = observe(dict(headers), load_key(dna.config_dir, dna.logger))

        sink.send(
            AuthEvidence.from_facts(
                facts,
                run_id=run_id,
                request_id=request_id,
                source=source,
                destination=str(host),
                auth_status=_auth_status(status),
                observed_at=time.time(),
            ),
            dna,
        )
    except Exception as exc:
        dna.logger.warning("agentdna.authevidence.outbound_failed", error=str(exc))


def _auth_status(status_code) -> str:
    """What the destination's answer says about the credential.

    Only a refusal is a verdict on the credential itself. A 5xx is the
    destination failing, a 404 is the wrong path, a redirect is neither -
    none of them judged the credential, and calling them "accepted" would
    claim more than was observed.
    """
    if status_code is None:
        return STATUS_UNKNOWN
    if status_code in (401, 403):
        return STATUS_REJECTED
    if 200 <= status_code < 300:
        return STATUS_ACCEPTED
    return STATUS_UNKNOWN


def _envelope_from_body(body) -> dict | None:
    """The envelope of the workflow this very call carries, or None.

    An MCP tool call already says which request it is: the client puts the
    signed workflow in `params._meta`. Reading it from there is what lets this
    record and the receiving server's record of the same request be compared -
    the server files under that same signature.

    Read from the wire rather than from the context on purpose. MCP clients
    call tools on a thread of their own, where a ContextVar set elsewhere is
    invisible; the bytes are not.

    None for everything else - a call carrying no workflow, a body too large to
    parse, a body we cannot read. The caller then falls back to the hop being
    served, which is the right anchor for a call to something that never sees a
    workflow at all.
    """
    if not isinstance(body, (bytes, str)) or not body or len(body) > MAX_BODY_BYTES:
        return None
    try:
        message = json.loads(body)
        meta = message["params"]["_meta"][AGENTDNA_META_KEY]
        workflow = json.loads(meta[AGENTDNA_INTENT_WORKFLOW_META_KEY])
        envelope = workflow["envelope"]
        return envelope if envelope.get("signature") else None
    except Exception:
        return None


def _current_dna():
    """The AgentDNA serving this request, for an observer installed without one.

    Same deferred import, for the same reason as `_current_envelope`.
    """
    try:
        from agentdna.mcp.context import get_context

        context = get_context()
        return context.dna if context is not None else None
    except Exception:
        return None


def _current_envelope():
    """The request this call is being made in service of, or None.

    Imported here rather than at the top: this module is about HTTP, and should
    load without the MCP package present.
    """
    try:
        from agentdna.mcp.context import get_context

        context = get_context()
        if context is None or not context.workflows:
            return None
        return context.workflows[0].get_latest_envelope()
    except Exception:
        return None
