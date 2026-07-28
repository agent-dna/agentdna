import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

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
    body = await request.json()

    try:
        result = await _get_cbac().verify_agent_app_interaction(
            agent_id=body.get("agent_id", ""),
            intended_action=body.get("intended_action"),
            user_intent=body.get("user_intent"),
        )
        decision, reason = result.decision, result.reason
    except Exception as exc:
        decision, reason = "error", str(exc)

    return PlainTextResponse(reason, headers={"X-CBAC-Decision": decision})


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("CBAC_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CBAC_SERVICE_PORT", "8767")),
    )
