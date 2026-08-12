import os
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from agentdna.provenance import Provenance
from cbac_service.cbac import CBAC
from cbac_service.db.engine import close_db, get_session
from cbac_service.logging_config import setup_logging

# ── App lifecycle ──────────────────────────────────────────────────────────────

setup_logging()
logger = structlog.get_logger("cbac_service.api")

_cbac: CBAC | None = None


def _get_cbac() -> CBAC:
    global _cbac
    if _cbac is None:
        provenance = Provenance(
            name="cbac-service",
            api_key=os.environ.get("AGENTDNA_API_KEY", ""),
        )
        _cbac = CBAC(provenance=provenance)
    return _cbac


def _bind_request_context(body: dict) -> None:
    """Bind the request identity so every log line from the decision pipeline
    below carries it without threading it through the call chain."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=uuid4().hex[:12],
        agent_id=body.get("agent_id", ""),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Shutdown: close the connection pool. The engine builds itself lazily."""
    yield
    await close_db()


app = FastAPI(lifespan=lifespan)

# ── Endpoints ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Basic healthcheck — verifies DB connectivity."""
    try:
        async with get_session() as session:
            await session.execute(text("SELECT 1"))
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


@app.post("/authorize-cbac")
async def authorize_cbac(request: Request) -> PlainTextResponse:
    """Decide whether an agent may perform an intended action.

    The body carries the reason and ``X-CBAC-Decision`` the verdict. The
    three component scores ride along as headers (omitted when the pipeline
    could not produce them) so the caller can feed them back to
    ``/compute-lhi`` once it knows whether the action succeeded.
    """
    body = await request.json()
    _bind_request_context(body)

    headers = {}
    try:
        async with get_session() as session:
            result = await _get_cbac().verify_cbac(
                session=session,
                agent_id=body.get("agent_id", ""),
                intended_action=body.get("intended_action"),
                user_intent=body.get("user_intent"),
            )
        decision, reason = result.decision, result.reason
        for header, score in (
            ("X-CBAC-Intent-Score", result.intent_score),
            ("X-CBAC-Policy-Score", result.policy_score),
            ("X-CBAC-Hallucination-Score", result.hallucination_score),
        ):
            if score is not None:
                headers[header] = str(score)
        logger.info(
            "cbac decision",
            decision=decision,
            reason=reason,
            intent_score=result.intent_score,
            policy_score=result.policy_score,
            hallucination_score=result.hallucination_score,
        )
    except Exception as exc:
        decision, reason = "error", str(exc)
        logger.exception("cbac authorization failed")

    return PlainTextResponse(reason, headers={"X-CBAC-Decision": decision, **headers})


@app.post("/compute-lhi")
async def compute_lhi(request: Request) -> JSONResponse:
    """Fold one completed interaction into the caller→callee trust score.

    The four component scores are supplied by the caller: the first three
    come back from its own ``/authorize-cbac`` response, ``output_score``
    from whether the action actually succeeded. Every call appends one
    ``lhi_records`` row, so the edge's full trust history is preserved.
    """
    body = await request.json()
    _bind_request_context(body)

    try:
        async with get_session() as session:
            trust = await _get_cbac().compute_lhi(
                session=session,
                agent_id=body.get("agent_id", ""),
                callee_name=body.get("callee_name", ""),
                callee_type=body.get("callee_type", ""),
                intent_score=body.get("intent_score"),
                policy_score=body.get("policy_score"),
                hallucination_score=body.get("hallucination_score"),
                output_score=body.get("output_score"),
            )
    except Exception as exc:
        logger.exception("lhi update failed", callee_name=body.get("callee_name", ""))
        return JSONResponse({"error": str(exc)}, status_code=500)

    logger.info("lhi trust updated", callee_name=body.get("callee_name", ""), trust=trust)
    return JSONResponse({"trust": trust})


@app.post("/precompute-policy")
async def precompute_policy(request: Request) -> JSONResponse:
    """Precompute and cache policy embeddings for an agent.

    Call this after deploying or updating an agent's policy card. Supply
    ``policy`` (the decoded policy text) to embed that instead of reading the
    agent's card from the Provenance Layer.
    """
    body = await request.json()
    agent_id = body.get("agent_id", "")
    policy = body.get("policy")

    if not agent_id:
        return JSONResponse({"error": "agent_id is required"}, status_code=400)
    if policy is not None and not isinstance(policy, str):
        return JSONResponse({"error": "policy must be a string"}, status_code=400)

    try:
        async with get_session() as session:
            count = await _get_cbac().index_policy(
                session=session,
                agent_id=agent_id,
                policy=policy,
            )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"agent_id": agent_id, "chunks_stored": count})


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("CBAC_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CBAC_SERVICE_PORT", "8767")),
        # None = keep our JSON config; uvicorn's default would re-add its own handlers.
        log_config=None,
    )
