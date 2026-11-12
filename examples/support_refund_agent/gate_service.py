"""The gate service Kong asks about every call. :8810

    python gate_service.py

The SDK's gate service, as the gateway's AgentDNA identity, with CBAC plugged in.
Only Kong should be able to reach it.
"""

import os

import uvicorn
from cbac import authorize
from dotenv import load_dotenv

from agentdna.core import AgentDNA
from agentdna.http.gate import create_app

load_dotenv()

# CBAC scores each call against what the person asked, so say what each app is for
APPS = {
    "stripe": "Refund a customer's Stripe payment. amount is in pence: 4000 is £40.",
    "jira": "Open a support ticket in Jira.",
}

# The gateway's identity: it signs every yes, no and answer
gateway = AgentDNA(
    name=os.getenv("GATEWAY_NAME", "Gateway"),
    type="tool",  # it sits in front of apps, like an MCP server
    api_key=os.environ["AGENTDNA_API_KEY"],
    provenance_layer_url=os.environ["PROVENANCE_LAYER_URL"],
    admin_server_url=os.environ["ADMIN_SERVER_URL"],
    log_level="ERROR",
)


async def cbac(agent_id, gateway_id, call, arguments, person_asked, description, intent_id):
    """Does this call fit what the person asked? The gate passes MCP's CBAC inputs."""
    app = call.split(" ", 1)[1].split("/")[1]  # "POST /stripe/v1/refunds" -> "stripe"
    return await authorize(
        agent_id,
        app,
        f"{app}: {call}",
        arguments,
        person_asked,
        APPS.get(app, ""),
        intent_id,
        callee_type="tool",
        cbac_url=os.getenv("CBAC_URL", "https://cbac-service.agentdna.io"),
        cbac_timeout=50,
    )


if __name__ == "__main__":
    print(f"gate service    :8810  {gateway.get_actor_id()}")
    uvicorn.run(create_app(gateway, cbac), host="127.0.0.1", port=8810, log_level="warning")
