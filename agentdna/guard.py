"""
Framework-agnostic CBAC guard layer.

This module provides the single Policy Enforcement Point (PEP) for
agent actions as a wrapper decorator, plus the ambient governance
context it reads from. It is deliberately independent of any agent
framework (LangGraph, CrewAI, MCP, ...): the guard wraps plain async
Python callables, which is the lowest common denominator every
framework ultimately dispatches to.

Design
------
Three kinds of input flow through three different channels:

1. Static config          -> decorator parameters (``@cbac_guard(...)``).
2. Call-time business args -> the wrapped function's own ``**kwargs``;
   the guard observes them to build the intent text. The caller (LLM /
   framework) passes nothing extra.
3. Ambient governance context (actor identity, envelope chain, root
   user intent) -> a ``contextvars.ContextVar`` set once at the request
   entry point via :func:`cbac_context`. It is never a function
   argument, so an LLM can neither supply nor forge it.

The context holds a *mutable* :class:`GovernanceContext`; guards update
``ctx.workflow`` in place as they append envelopes, so the evolving
chain is visible across the whole call tree (nested and subsequent
calls alike) without being threaded through signatures.

Every guarded call is bracketed the same way the example apps used to
hand-write per tool:

    outbound envelope -> authorize (CBAC) -> execute -> outcome envelope

Decisions come from one of two engines (see :func:`configure`):

- ``mode="remote"``: the CBAC admin service authorizes AND executes the
  HTTP request (the proxy holds the flow). Guarded functions must be
  pure request builders returning :class:`AppRequest`.
- ``mode="local"``: the three-tier semantic pipeline in
  :mod:`agentdna.cbac` decides (imported lazily -- it needs the optional
  ``[semantic]`` ML dependencies), then the guard executes the
  :class:`AppRequest` itself, or runs a plain callable directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json

from dataclasses import asdict, dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    Optional,
    Tuple,
    TYPE_CHECKING,
)

from .helpers import get_envelope_depth, parse_workflow
from .types import Actor, IntentWorkflow, Issue, VerificationResult

if TYPE_CHECKING:
    from .core import AgentDNA


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class AppRequest:
    """An external HTTP action a guarded function wants performed.

    A guarded function that returns an ``AppRequest`` is an "HTTP tool":
    its body is a pure request builder with no side effects, and the
    guard performs the request after authorization (locally, or via the
    remote CBAC proxy which decides and executes in one step).
    """

    url: str
    method: str = "POST"
    headers: Dict[str, str] = field(default_factory=dict)
    body: str | dict | None = None
    timeout: float = 30.0


@dataclass
class GovernanceContext:
    """Mutable per-request governance state shared by every guard.

    Guards replace ``workflow`` under ``lock`` as envelopes are
    appended. Because callers share this holder object (not a snapshot),
    updates made inside nested calls are visible to the enclosing scope.
    """

    actor: "AgentDNA"
    workflow: IntentWorkflow
    user_intent: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_governance_ctx: contextvars.ContextVar[Optional[GovernanceContext]] = contextvars.ContextVar(
    "agentdna_governance_ctx", default=None
)


def get_context() -> Optional[GovernanceContext]:
    """Return the ambient GovernanceContext, or None when governance is off."""
    return _governance_ctx.get()


@contextlib.contextmanager
def cbac_context(
    actor: "AgentDNA",
    workflow: IntentWorkflow,
    user_intent: str = "",
) -> Iterator[GovernanceContext]:
    """Open a governance scope. Set once at the request entry point.

    Every ``@cbac_guard``-wrapped callable invoked inside this block
    reads the context ambiently; the caller passes nothing per call.
    On exit, ``ctx.workflow`` holds the full envelope chain including
    everything the guards appended.
    """
    holder = GovernanceContext(
        actor=actor,
        workflow=workflow,
        user_intent=user_intent,
    )
    token = _governance_ctx.set(holder)
    try:
        yield holder
    finally:
        _governance_ctx.reset(token)


# ── Layer configuration ───────────────────────────────────────────────────────


@dataclass
class GuardConfig:
    mode: str = "remote"  # "remote" | "local"
    cbac_url: str = "https://cbac-admin.agentdna.io"
    cbac_timeout: float = 100.0
    advise_action: str = "deny"  # local-mode "advise" mapping; fail-closed


_config = GuardConfig()


def configure(
    mode: Optional[str] = None,
    cbac_url: Optional[str] = None,
    cbac_timeout: Optional[float] = None,
    advise_action: Optional[str] = None,
) -> None:
    """Set layer-wide guard configuration. Call once at startup."""
    if mode is not None:
        if mode not in ("remote", "local"):
            raise ValueError(f"unsupported guard mode: {mode!r}")
        _config.mode = mode
    if cbac_url is not None:
        _config.cbac_url = cbac_url
    if cbac_timeout is not None:
        _config.cbac_timeout = cbac_timeout
    if advise_action is not None:
        if advise_action not in ("deny", "allow"):
            raise ValueError(f"unsupported advise_action: {advise_action!r}")
        _config.advise_action = advise_action


def get_config() -> GuardConfig:
    return _config


# ── Workflow (de)serialization ────────────────────────────────────────────────


def serialize_workflow(workflow: IntentWorkflow) -> str:
    return json.dumps(
        asdict(workflow),
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_workflow(data: str) -> IntentWorkflow:
    return parse_workflow(json.loads(data))


# ── Guard internals ───────────────────────────────────────────────────────────


def _default_intent(app_name: str, action_name: str, kwargs: Dict[str, Any]) -> str:
    parts = [f"{app_name}:{action_name}"]
    parts.extend(f"{k}={str(v)[:200]}" for k, v in kwargs.items())
    return " ".join(parts)


def _default_describe(action_name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {"action": action_name, **{k: str(v)[:200] for k, v in kwargs.items()}}


def _bind_kwargs(sig: inspect.Signature, args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Flatten positional + keyword call args into a name->value dict."""
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        # Let the wrapped function raise its own error on the real call.
        return dict(kwargs)


def _is_http_tool(sig: inspect.Signature) -> bool:
    ann = sig.return_annotation
    return ann is AppRequest or ann == "AppRequest"


async def _execute_request(request: AppRequest) -> Tuple[int, str, dict]:
    """Perform an AppRequest with httpx. Returns (status_code, text, json)."""
    import httpx  # deliberately lazy: not a hard dependency of the library

    async with httpx.AsyncClient(timeout=request.timeout) as client:
        if isinstance(request.body, (dict, list)):
            response = await client.request(
                request.method,
                request.url,
                json=request.body,
                headers=request.headers,
            )
        else:
            response = await client.request(
                request.method,
                request.url,
                content=request.body,
                headers=request.headers,
            )

    try:
        data = response.json()
    except Exception:
        data = {}

    return response.status_code, response.text, data


def _shape_http_result(
    status_code: int,
    text: str,
    data: dict,
    parse_response: Optional[Callable[[dict, int], dict]],
) -> Dict[str, Any]:
    if status_code >= 300:
        return {"status": "failed", "http_status": status_code, "error": text}
    parsed = parse_response(data, status_code) if parse_response else {"response": data}
    return {"status": "created", **parsed}


# Local-mode CBAC engines are expensive to construct (model loading);
# cache one per actor DID.
_LOCAL_CBAC_CACHE: Dict[str, Any] = {}


async def _local_authorize(
    ctx: GovernanceContext,
    intent_text: str,
    cfg: GuardConfig,
) -> Tuple[str, str]:
    """Decide via the local three-tier pipeline. Returns (decision, detail)."""
    from .cbac import CBAC  # lazy: needs the optional [semantic] deps

    actor_id = ctx.actor.get_actor_id()
    cbac = _LOCAL_CBAC_CACHE.get(actor_id)
    if cbac is None:
        cbac = CBAC(provenance=ctx.actor.provenance, cbac_url=cfg.cbac_url)
        _LOCAL_CBAC_CACHE[actor_id] = cbac

    result = await cbac.verify_agent_app_interaction(
        agent_id=actor_id,
        intended_action=intent_text,
        user_intent=ctx.user_intent or None,
    )

    decision = result.decision
    if decision == "advise":
        decision = cfg.advise_action

    return decision, result.reason


def _remote_authorize_and_execute_sync(
    agent_id: str,
    intent_text: str,
    envelope_dict: dict,
    request: AppRequest,
    cfg: GuardConfig,
):
    """POST to the CBAC admin service, which decides AND executes the request.

    Inlined equivalent of ``CBAC.authorise_agent_app_interaction``
    (agentdna/cbac.py); duplicated here because importing that module
    pulls in the optional sentence-transformers dependency, which
    remote mode must not require.
    """
    import requests  # lazy; transitive dependency of the library already

    body = request.body
    headers = dict(request.headers)
    if isinstance(body, dict):
        body = json.dumps(body)
        headers = {"Content-Type": "application/json", **headers}

    payload = {
        "agent_id": agent_id,
        "action_intent": intent_text,
        "envelope": envelope_dict,
        "app_request": {
            "url": request.url,
            "method": request.method,
            "headers": headers,
            "body": body or "",
        },
    }

    response = requests.post(
        f"{cfg.cbac_url.rstrip('/')}/agent-admin/v1/authorize-action",
        json=payload,
        timeout=cfg.cbac_timeout,
    )

    decision = response.headers.get("X-CBAC-Decision", "allow")
    return decision, response


def _app_actor(app_name: str) -> Actor:
    return Actor(id="", name=app_name, type="app")


async def _append_outbound(
    ctx: GovernanceContext,
    app_name: str,
    payload: Dict[str, Any],
) -> None:
    async with ctx.lock:
        ctx.workflow = ctx.actor.build(
            recipient_actor_id="",
            recipient_actor_name=app_name,
            recipient_actor_type="app",
            payload=json.dumps(payload),
            workflow=ctx.workflow,
        )


async def _append_outcome(
    ctx: GovernanceContext,
    app_name: str,
    payload: Dict[str, Any],
    issue_reason: Optional[str] = None,
) -> None:
    """Append an app->actor outcome envelope; an issue marks it invalid."""
    async with ctx.lock:
        verification_result = None
        if issue_reason is not None:
            depth = get_envelope_depth(ctx.workflow.envelope)
            verification_result = VerificationResult(
                valid=False,
                chain_depth=depth + 1,
                issues=[Issue(depth=depth + 1, reason=issue_reason)],
            )

        ctx.workflow = ctx.actor.build(
            recipient_actor_id=ctx.actor.get_actor_id(),
            recipient_actor_name=ctx.actor.name,
            recipient_actor_type=ctx.actor.type,
            payload=json.dumps(payload),
            verification_result=verification_result,
            workflow=ctx.workflow,
            from_actor=_app_actor(app_name),
        )


# ── The decorator ─────────────────────────────────────────────────────────────


def cbac_guard(
    *,
    app_name: str = "app",
    action: Optional[str] = None,
    describe: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    action_intent: Optional[Callable[[Dict[str, Any]], str]] = None,
    parse_response: Optional[Callable[[dict, int], dict]] = None,
    on_deny: str = "return",
):
    """Guard an async callable with CBAC authorization + envelope attestation.

    Parameters
    ----------
    app_name:
        The external system the action targets; becomes the recipient of
        the outbound envelope and the ``from_`` actor of the outcome one.
    action:
        Logical action name; defaults to the function name.
    describe:
        ``kwargs -> dict`` payload builder for the outbound envelope.
        Defaults to ``{"action": <name>, **truncated kwargs}``.
    action_intent:
        ``kwargs -> str`` intent text handed to CBAC. Defaults to
        ``"<app>:<action> k=v ..."``.
    parse_response:
        ``(json, status_code) -> dict`` extracting the fields recorded in
        the success envelope and returned to the caller (HTTP tools only).
    on_deny:
        ``"return"`` -> a denied call returns ``{"status": "denied", ...}``
        (readable by an LLM loop); ``"raise"`` -> raises PermissionError.

    Behavior
    --------
    - No ambient context (:func:`cbac_context` not open): governance is
      disabled; the call passes through (AppRequests are still executed).
    - HTTP tool (returns :class:`AppRequest`, detected via the return
      annotation): outbound envelope -> authorize (local) or
      authorize-and-execute (remote) -> outcome envelope.
    - Plain callable: local mode authorizes *before* executing the body;
      remote mode refuses up front (the remote engine has no
      decision-only form, and the body may have side effects).
    """
    if on_deny not in ("return", "raise"):
        raise ValueError(f"unsupported on_deny: {on_deny!r}")

    def decorate(fn: Callable[..., Awaitable[Any]]):
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"cbac_guard requires an async function, got {fn!r}")

        action_name = action or fn.__name__
        sig = inspect.signature(fn)
        http_tool = _is_http_tool(sig)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = get_context()
            call_kwargs = _bind_kwargs(sig, args, kwargs)

            # Governance disabled: pass through (still executing AppRequests).
            if ctx is None:
                result = await fn(*args, **kwargs)
                if isinstance(result, AppRequest):
                    status_code, text, data = await _execute_request(result)
                    return _shape_http_result(status_code, text, data, parse_response)
                return result

            cfg = get_config()

            if not http_tool and cfg.mode == "remote":
                raise RuntimeError(
                    f"cbac_guard: {action_name!r} is a plain callable; "
                    "mode='remote' only supports AppRequest-returning tools "
                    "(the remote engine authorizes and executes in one step)"
                )

            intent_text = (
                action_intent(call_kwargs)
                if action_intent
                else _default_intent(app_name, action_name, call_kwargs)
            )

            outbound_payload = (
                describe(call_kwargs) if describe else _default_describe(action_name, call_kwargs)
            )
            await _append_outbound(ctx, app_name, outbound_payload)

            # ── Plain callable, local mode: authorize BEFORE executing. ──
            if not http_tool:
                try:
                    decision, detail = await _local_authorize(ctx, intent_text, cfg)
                except Exception as exc:
                    decision, detail = "error", str(exc)
                if decision != "allow":
                    return await _deny_or_error(ctx, decision, detail)

                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    await _append_outcome(
                        ctx,
                        app_name,
                        {"status": "error"},
                        issue_reason=str(exc),
                    )
                    return {"status": "error", "error": str(exc)}

                payload = result if isinstance(result, dict) else {"result": str(result)[:500]}
                await _append_outcome(ctx, app_name, {"status": "completed", **payload})
                return result

            # ── HTTP tool: fn is a pure request builder. ──
            try:
                request = await fn(*args, **kwargs)
            except Exception as exc:
                await _append_outcome(
                    ctx,
                    app_name,
                    {"status": "error"},
                    issue_reason=str(exc),
                )
                return {"status": "error", "error": str(exc)}

            if not isinstance(request, AppRequest):
                raise RuntimeError(
                    f"cbac_guard: {action_name!r} is annotated -> AppRequest "
                    f"but returned {type(request).__name__}"
                )

            decision, detail = "allow", ""
            status_code, text, data = 0, "", {}
            try:
                if cfg.mode == "remote":
                    async with ctx.lock:
                        envelope_dict = asdict(ctx.workflow)
                    decision, response = await asyncio.to_thread(
                        _remote_authorize_and_execute_sync,
                        ctx.actor.get_actor_id(),
                        intent_text,
                        envelope_dict,
                        request,
                        cfg,
                    )
                    if decision in ("deny", "error"):
                        detail = response.text
                    else:
                        status_code, text = response.status_code, response.text
                        try:
                            data = response.json()
                        except Exception:
                            data = {}
                else:
                    decision, detail = await _local_authorize(ctx, intent_text, cfg)
                    if decision == "allow":
                        status_code, text, data = await _execute_request(request)
            except Exception as exc:
                await _append_outcome(
                    ctx,
                    app_name,
                    {"status": "error"},
                    issue_reason=str(exc),
                )
                return {"status": "error", "error": str(exc)}

            if decision != "allow":
                return await _deny_or_error(ctx, decision, detail)

            if status_code >= 300:
                await _append_outcome(
                    ctx,
                    app_name,
                    {"status": "failed", "http_status": status_code},
                    issue_reason=text[:500],
                )
                return {"status": "failed", "http_status": status_code, "error": text}

            parsed = parse_response(data, status_code) if parse_response else {"response": data}
            await _append_outcome(ctx, app_name, {"status": "created", **parsed})
            return {"status": "created", **parsed}

        async def _deny_or_error(ctx: GovernanceContext, decision: str, detail: str):
            if decision == "deny":
                await _append_outcome(
                    ctx,
                    app_name,
                    {"status": "denied", "reason": detail},
                    issue_reason=f"cbac denied: {detail}",
                )
                if on_deny == "raise":
                    raise PermissionError(detail)
                return {"status": "denied", "error": detail}

            await _append_outcome(
                ctx,
                app_name,
                {"status": "error"},
                issue_reason=detail,
            )
            return {"status": "error", "error": detail}

        # Frameworks introspect the signature to build LLM-facing tool
        # schemas; the guard must be invisible there, and the visible
        # return type is the shaped dict, not AppRequest.
        wrapper.__signature__ = sig.replace(return_annotation=dict)
        wrapper.__annotations__ = {**getattr(fn, "__annotations__", {}), "return": dict}

        return wrapper

    return decorate
