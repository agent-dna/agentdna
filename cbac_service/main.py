import asyncio
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from agentdna.provenance import Provenance
from cbac_service.cbac import CBAC

# ── HTTP boundary ──────────────────────────────────────────────────────────────

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


app = FastAPI()


@app.post("/authorize-cbac")
async def authorize_cbac(request: Request) -> PlainTextResponse:
    """Decide whether an agent may perform an intended action.

    The body carries the reason and ``X-CBAC-Decision`` the verdict. The
    three component scores ride along as headers (omitted when the pipeline
    could not produce them) so the caller can feed them back to
    ``/compute-lhi`` once it knows whether the action succeeded.
    """
    body = await request.json()

    headers = {}
    try:
        result = await _get_cbac().verify_cbac(
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
    except Exception as exc:
        decision, reason = "error", str(exc)

    return PlainTextResponse(reason, headers={"X-CBAC-Decision": decision, **headers})


@app.post("/compute-lhi")
async def compute_lhi(request: Request) -> JSONResponse:
    """Fold one completed interaction into the caller→callee trust score.

    The four component scores are supplied by the caller: the first three
    come back from its own ``/authorize-cbac`` response, ``output_score``
    from whether the action actually succeeded. Nothing here re-verifies
    them, so a caller that fabricates scores can inflate its own
    reputation — the guard is already trusted to make the authorize call
    at all, and this endpoint inherits exactly that much trust.
    """
    body = await request.json()

    try:
        # compute_lhi is sync and writes to the Provenance Layer — off the loop.
        trust = await asyncio.to_thread(
            _get_cbac().compute_lhi,
            agent_id=body.get("agent_id", ""),
            callee_name=body.get("callee_name", ""),
            callee_type=body.get("callee_type", ""),
            intent_score=body.get("intent_score"),
            policy_score=body.get("policy_score"),
            hallucination_score=body.get("hallucination_score"),
            output_score=body.get("output_score"),
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"trust": trust})


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("CBAC_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CBAC_SERVICE_PORT", "8767")),
    )
