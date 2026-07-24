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

from dataclasses import dataclass
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

if TYPE_CHECKING:
    from .core import AgentDNA


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class GovernanceContext:
    """Per-request governance state the guard authorizes against.

    The guard reads ``actor`` (for the agent id) and ``user_intent`` --
    the only two inputs the CBAC call needs beyond the intended action.
    """

    actor: "AgentDNA"
    user_intent: str = ""


_governance_ctx: contextvars.ContextVar[Optional[GovernanceContext]] = contextvars.ContextVar(
    "agentdna_governance_ctx", default=None
)


def get_context() -> Optional[GovernanceContext]:
    """Return the ambient GovernanceContext, or None when governance is off."""
    return _governance_ctx.get()

#TODO:- instead of AgentDNA capture agent_id. 
@contextlib.contextmanager
def cbac_context(
    actor: "AgentDNA",
    user_intent: str = "",
) -> Iterator[GovernanceContext]:
    """Open a governance scope. Set once at the request entry point.

    Every ``@cbac_guard``-wrapped callable invoked inside this block
    reads the context ambiently; the caller passes nothing per call.
    """
    holder = GovernanceContext(actor=actor, user_intent=user_intent)
    token = _governance_ctx.set(holder)
    try:
        yield holder
    finally:
        _governance_ctx.reset(token)


# ── Layer configuration ───────────────────────────────────────────────────────

#TODO:- Check if we can remove advise_action?
@dataclass
class GuardConfig:
    cbac_url: str = "https://cbac-admin.agentdna.io"
    cbac_timeout: float = 100.0
    advise_action: str = "deny" 


_config = GuardConfig()


def configure(
    cbac_url: Optional[str] = None,
    cbac_timeout: Optional[float] = None,
    advise_action: Optional[str] = None,
) -> None:
    """Set layer-wide guard configuration. Call once at startup."""
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


# ── Guard internals ───────────────────────────────────────────────────────────


def _default_intent(action_name: str, kwargs: Dict[str, Any]) -> str:
    """Build the CBAC intent text from the call's own arguments."""
    parts = [action_name]
    parts.extend(f"{k}={str(v)[:200]}" for k, v in kwargs.items())
    return " ".join(parts)


def _bind_kwargs(sig: inspect.Signature, args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Flatten positional + keyword call args into a name->value dict."""
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        # Let the wrapped function raise its own error on the real call.
        return dict(kwargs)


def _authorize_sync(
    agent_id: str,
    intended_action: Any,
    user_intent: Optional[str],
    cfg: GuardConfig,
) -> Tuple[str, str]:
    """POST to the CBAC decision service.

    The reference implementation (agentdna/cbac_service.py) runs
    ``verify_agent_app_interaction`` behind this endpoint and returns a
    decision only -- it never executes the action. The payload is
    exactly that method's inputs.
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
    return decision, response.text

#TODO:- Verify return type. 
async def _authorize(
    ctx: GovernanceContext,
    intent_text: str,
    cfg: GuardConfig,
) -> Tuple[str, str]:
    decision, detail = await asyncio.to_thread(
        _authorize_sync,
        ctx.actor.get_actor_id(),
        intent_text,
        ctx.user_intent or None,
        cfg,
    )
    if decision == "advise":
        decision = cfg.advise_action
    return decision, detail


# ── The decorator ─────────────────────────────────────────────────────────────


def cbac_guard(
    *,
    action: Optional[str] = None,
    action_intent: Optional[Callable[[Dict[str, Any]], str]] = None,
    on_deny: str = "return",
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
      short-circuits -> run the wrapped function and return its result.
    """
    if on_deny not in ("return", "raise"):
        raise ValueError(f"unsupported on_deny: {on_deny!r}")

    def decorate(fn: Callable[..., Awaitable[Any]]):
        if not asyncio.iscoroutinefunction(fn):
            raise TypeError(f"cbac_guard requires an async function, got {fn!r}")

        action_name = action or fn.__name__
        sig = inspect.signature(fn)

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
                else _default_intent(action_name, call_kwargs)
            )

            try:
                decision, detail = await _authorize(ctx, intent_text, get_config())
            except Exception as exc:
                decision, detail = "error", str(exc)

            if decision != "allow":
                if decision == "deny" and on_deny == "raise":
                    raise PermissionError(detail)
                status = "denied" if decision == "deny" else "error"
                return {"status": status, "error": detail}

            return await fn(*args, **kwargs)

        # Frameworks (LangChain / MCP) build the LLM-facing tool schema by
        # introspecting the callable's signature. functools.wraps copies
        # __name__/__doc__/__annotations__ but leaves the wrapper's own
        # ``(*args, **kwargs)`` in place, and not every schema generator
        # follows ``__wrapped__``. Pin the wrapped function's real
        # signature so the generated schema stays correct.
        wrapper.__signature__ = sig  # pyright: ignore[reportAttributeAccessIssue]

        return wrapper

    return decorate
