"""
Framework-agnostic CBAC guard layer.

This module provides the single Policy Enforcement Point (PEP) for
agent actions as a wrapper decorator, plus the ambient governance
context it reads from. It is deliberately independent of any agent
framework (LangGraph, CrewAI, MCP, ...): the guard wraps plain async
Python callables, which is the lowest common denominator every
framework ultimately dispatches to.

The guard does one thing: authorize. It turns a call into an intent,
asks the CBAC decision service (one HTTP call), and either lets the
wrapped function run or short-circuits a denial. It does **not** build
provenance envelopes and does **not** perform the action itself:

    intent (from call args) -> authorize (CBAC) -> run the wrapped fn

Attestation (build/handle envelopes) is handled separately at the
workflow's delegation boundaries, and the wrapped function does its own
work -- e.g. a GitHub tool makes its own HTTP request and returns a
result dict.

Two kinds of input flow through two channels:

1. Static config          -> decorator parameters (``@cbac_guard(...)``).
2. Ambient governance context (actor identity, root user intent) -> a
   ``contextvars.ContextVar`` set once at the request entry point via
   :func:`cbac_context`. It is never a function argument, so an LLM can
   neither supply nor forge it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import (
    Any,
)

# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class GovernanceContext:
    """Per-request governance state the guard authorizes against.

    ``agent_id`` (whose policy is checked) and ``user_intent`` are the
    only two inputs the CBAC call needs beyond the intended action.
    """

    agent_id: str
    user_intent: str = ""


_governance_ctx: contextvars.ContextVar[GovernanceContext | None] = contextvars.ContextVar(
    "agentdna_governance_ctx", default=None
)


def get_context() -> GovernanceContext | None:
    """Return the ambient GovernanceContext, or None when governance is off."""
    return _governance_ctx.get()


@contextlib.contextmanager
def cbac_context(
    agent_id: str,
    user_intent: str = "",
) -> Iterator[GovernanceContext]:
    """Open a governance scope. Set once at the request entry point.

    Every ``@cbac_guard``-wrapped callable invoked inside this block
    reads the context ambiently; the caller passes nothing per call.
    """
    holder = GovernanceContext(agent_id=agent_id, user_intent=user_intent)
    token = _governance_ctx.set(holder)
    try:
        yield holder
    finally:
        _governance_ctx.reset(token)


# ── Layer configuration ───────────────────────────────────────────────────────


@dataclass
class GuardConfig:
    cbac_url: str = "https://cbac-admin.agentdna.io"
    cbac_timeout: float = 100.0


_config = GuardConfig()


def configure(
    cbac_url: str | None = None,
    cbac_timeout: float | None = None,
) -> None:
    """Set layer-wide guard configuration. Call once at startup."""
    if cbac_url is not None:
        _config.cbac_url = cbac_url
    if cbac_timeout is not None:
        _config.cbac_timeout = cbac_timeout


def get_config() -> GuardConfig:
    return _config


# ── Guard internals ───────────────────────────────────────────────────────────


def _action_summary(action_name: str, description: str | None = None) -> str:
    """Verb-only prose for the intended action: "The agent wants to <verb>."

    The verb phrase is the tool's own description (docstring first line)
    when available, else the de-snaked tool name. This is the prefix of
    :func:`_default_intent`'s full rendering; kept as its own function so a
    verb-only view stays available to callers that want one.
    """
    verb = (description or action_name.replace("_", " ")).strip().rstrip(".")
    return f"The agent wants to {verb[:1].lower()}{verb[1:]}."


def _default_intent(
    action_name: str, kwargs: dict[str, Any], description: str | None = None
) -> str:
    """Render the intended action as prose, parameters included.

    NLI and the policy tiers score natural language far better than raw
    ``tool k=v`` call syntax — snake_case tool names hide their verb from
    the NLI model (measured: a direct "close" vs "do not close"
    contradiction scored 0.01 on call syntax, 0.95 as prose).
    """
    text = _action_summary(action_name, description).rstrip(".")
    if kwargs:
        text += ", with " + ", ".join(f"{k} = {str(v)}" for k, v in kwargs.items())
    return text + "."


def _bind_kwargs(sig: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any]:
    """Flatten positional + keyword call args into a name->value dict."""
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        # Let the wrapped function raise its own error on the real call.
        return dict(kwargs)


_SCORE_HEADERS = {
    "intent_score": "X-CBAC-Intent-Score",
    "policy_score": "X-CBAC-Policy-Score",
    "hallucination_score": "X-CBAC-Hallucination-Score",
}

# Tools that report failure by return value rather than by raising.
_FAILED_STATUSES = frozenset({"error", "denied", "failed"})


def _authorize_sync(
    agent_id: str,
    intended_action: Any,
    user_intent: str | None,
    cfg: GuardConfig,
) -> tuple[str, str, dict[str, float | None]]:
    """POST to the CBAC decision service.

    The reference implementation (the cbac_service package) runs
    ``verify_cbac`` behind this endpoint and returns a
    decision only -- it never executes the action. The payload is
    exactly that method's inputs.

    Alongside the decision the service returns the three component scores
    it computed; they come back as headers and are carried here so the
    caller can report them to ``/compute-lhi`` once the action's outcome
    is known. A score the pipeline could not produce is absent.
    """
    import requests  # lazy; transitive dependency of the library already

    payload = {
        "agent_id": agent_id,
        "intended_action": intended_action,
        "user_intent": user_intent,
    }

    response = requests.post(
        f"{cfg.cbac_url.rstrip('/')}/authorize-cbac",
        json=payload,
        timeout=cfg.cbac_timeout,
    )

    decision = response.headers.get("X-CBAC-Decision", "advise")

    scores: dict[str, float | None] = {}
    for name, header in _SCORE_HEADERS.items():
        raw = response.headers.get(header)
        try:
            scores[name] = float(raw) if raw is not None else None
        except ValueError:
            scores[name] = None

    return decision, response.text, scores


async def _authorize(
    ctx: GovernanceContext,
    intent_text: str,
    cfg: GuardConfig,
) -> tuple[str, str, dict[str, float | None]]:
    decision, detail, scores = await asyncio.to_thread(
        _authorize_sync,
        ctx.agent_id,
        intent_text,
        ctx.user_intent or None,
        cfg,
    )
    return decision, detail, scores


def _output_score(result: Any) -> float:
    """1.0 when the call succeeded, 0.0 when it reported failure.

    Raising is handled by the caller; this only inspects a returned value,
    since plenty of tools signal failure with a status field instead.
    """
    if isinstance(result, dict) and result.get("status") in _FAILED_STATUSES:
        return 0.0
    return 1.0


def _report_lhi_sync(
    agent_id: str,
    callee_name: str,
    callee_type: str,
    scores: dict[str, float | None],
    output_score: float,
    cfg: GuardConfig,
) -> None:
    """POST the completed interaction's four scores to the CBAC service."""
    import requests

    payload = {
        "agent_id": agent_id,
        "callee_name": callee_name,
        "callee_type": callee_type,
        "output_score": output_score,
        **scores,
    }

    requests.post(
        f"{cfg.cbac_url.rstrip('/')}/compute-lhi",
        json=payload,
        timeout=cfg.cbac_timeout,
    )


async def _report_lhi(
    ctx: GovernanceContext,
    callee_name: str,
    callee_type: str,
    scores: dict[str, float | None],
    output_score: float,
    cfg: GuardConfig,
) -> None:
    """Fold this interaction into the agent's trust score, best-effort.

    Skipped entirely when the authorize step could not produce all three
    component scores -- a trust record built on substituted values would
    be indistinguishable from a measured one.

    Never raises: the action has already run and its side effects have
    happened, so a failure to record trust must not destroy the result the
    caller is waiting for (nor mask the exception it is about to see).
    """
    if any(scores.get(name) is None for name in _SCORE_HEADERS):
        return

    try:
        await asyncio.to_thread(
            _report_lhi_sync,
            ctx.agent_id,
            callee_name,
            callee_type,
            scores,
            output_score,
            cfg,
        )
    except Exception:
        pass


# ── Framework-agnostic call gate ──────────────────────────────────────────────
#
# The authorize/report flow every framework needs, split from any framework's
# request/result types. A framework interceptor supplies only its own glue:
# extract (name, args), execute, detect success, render a denial. See
# `agentdna.cbac.mcp.cbac_intercept` for the MCP/LangChain adapter.


async def authorize_tool_call(
    callee_name: str,
    args: dict[str, Any],
    description: str | None = None,
) -> tuple[str, str, dict[str, float | None]]:
    """Authorize one tool call against the ambient policy (decision only).

    Returns ``(decision, detail, scores)``. ``decision`` is ``"allow"`` when
    governance is off (no :func:`cbac_context`), so a caller can always
    proceed on ``"allow"`` and short-circuit otherwise. Fail-closed: any
    error resolves to ``"error"`` and never raises.

    ``description`` is the tool's own description (schema/docstring first
    line) used as the verb phrase of the rendered intent; without it the
    de-snaked ``callee_name`` is the fallback.
    """
    ctx = get_context()
    if ctx is None:
        return "allow", "", {}
    intent = _default_intent(callee_name, args, description)
    cfg = get_config()
    try:
        return await _authorize(ctx, intent, cfg)
    except Exception as exc:
        return "error", str(exc), {}


async def report_tool_outcome(
    callee_name: str,
    scores: dict[str, float | None],
    *,
    ok: bool,
    callee_type: str = "tool",
) -> None:
    """Fold a finished tool call's outcome into the trust score. No-op when
    governance is off; never raises (mirrors :func:`_report_lhi`)."""
    ctx = get_context()
    if ctx is None:
        return
    await _report_lhi(ctx, callee_name, callee_type, scores, 1.0 if ok else 0.0, get_config())


# ── The decorator ─────────────────────────────────────────────────────────────


def cbac_guard(
    *,
    action: str | None = None,
    action_intent: Callable[[dict[str, Any]], str] | None = None,
    on_deny: str = "return",
    callee_type: str = "tool",
):
    """Guard an async callable with a CBAC authorization gate.

    Parameters
    ----------
    action:
        Logical action name used to label the intent; defaults to the
        function name.
    action_intent:
        ``kwargs -> str`` builder for the intent handed to CBAC. Defaults
        to the action name followed by the call's arguments.
    on_deny:
        ``"return"`` -> a denied call returns ``{"status": "denied", ...}``
        (readable by an LLM loop); ``"raise"`` -> raises PermissionError.
    callee_type:
        What is on the other end of this edge (``"tool"``, ``"agent"``,
        ``"mcp"``), recorded with the trust score.

    Behavior
    --------
    - No ambient context (:func:`cbac_context` not open): governance is
      not enabled for this call path, so the guard is a no-op and the
      wrapped function runs unguarded. Opening a context is a deployment
      decision an LLM cannot make, so this can never bypass a guard that
      *is* active -- it only expresses "this path opted out of
      governance", the same way a route without rate-limiting opts out.
    - Otherwise: intent (from the call's arguments) -> authorize (one HTTP
      call to the CBAC decision service) -> a non-allow decision
      short-circuits -> run the wrapped function -> report the outcome as
      a trust score -> return the result.

    A denied call never runs, so it produces no trust record; the gate
    already blocked it.
    """
    if on_deny not in ("return", "raise"):
        raise ValueError(f"unsupported on_deny: {on_deny!r}")

    def decorate(fn: Callable[..., Awaitable[Any]]):
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"cbac_guard requires an async function, got {fn!r}")

        action_name = action or fn.__name__
        sig = inspect.signature(fn)
        doc = inspect.getdoc(fn)
        description = doc.splitlines()[0].strip() if doc else None

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = get_context()

            # No governance scope open -> governance is opt-in and this
            # path opted out; run the function exactly as if unguarded.
            if ctx is None:
                return await fn(*args, **kwargs)

            call_kwargs = _bind_kwargs(sig, args, kwargs)
            intent_text = (
                action_intent(call_kwargs)
                if action_intent
                else _default_intent(action_name, call_kwargs, description)
            )

            cfg = get_config()
            scores: dict[str, float | None] = {}
            try:
                decision, detail, scores = await _authorize(ctx, intent_text, cfg)
            except Exception as exc:
                decision, detail = "error", str(exc)

            if decision != "allow":
                if decision == "deny" and on_deny == "raise":
                    raise PermissionError(detail)
                status = "denied" if decision == "deny" else "error"
                return {"status": status, "error": detail}

            try:
                result = await fn(*args, **kwargs)
            except Exception:
                await _report_lhi(ctx, action_name, callee_type, scores, 0.0, cfg)
                raise

            await _report_lhi(ctx, action_name, callee_type, scores, _output_score(result), cfg)
            return result

        # Frameworks (LangChain / MCP) build the LLM-facing tool schema by
        # introspecting the callable's signature. functools.wraps copies
        # __name__/__doc__/__annotations__ but leaves the wrapper's own
        # ``(*args, **kwargs)`` in place, and not every schema generator
        # follows ``__wrapped__``. Pin the wrapped function's real
        # signature so the generated schema stays correct.
        wrapper.__signature__ = sig  # pyright: ignore[reportAttributeAccessIssue]

        return wrapper

    return decorate
