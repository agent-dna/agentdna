"""The support lead: asks the Support Agent to refund a double charge, and log it.

    python support_lead.py          # order 4821: a normal refund
    python support_lead.py 4822     # order 4822: the customer's email tries a prompt injection

She sends no key anywhere. The agents hold none either: Kong adds Stripe's and
Jira's keys, after checking each call.
"""

import os
import sys

from dotenv import load_dotenv

from agentdna.core import AgentDNA
from agentdna.http import SignedClient

load_dotenv()

SUPPORT_AGENT_URL = "http://127.0.0.1:8811"
ORDER = sys.argv[1] if len(sys.argv) > 1 else "4821"
REQUEST = (
    f"Order #{ORDER} was charged twice, £40 each (payment {os.environ['STRIPE_PAYMENT_INTENT']}). "
    "Refund the duplicate £40 and log a ticket."
)

# 1. Who I am. Without agentdna: not needed
lead = AgentDNA(
    name=os.getenv("SUPPORT_LEAD_NAME", "support-lead"),
    type="user",
    api_key=os.environ["AGENTDNA_API_KEY"],
    provenance_layer_url=os.environ["PROVENANCE_LAYER_URL"],
    admin_server_url=os.environ["ADMIN_SERVER_URL"],
    log_level="ERROR",
)

print(f"Support lead: {lead.get_actor_id()}")

# 2. Ask the Support Agent. The call is signed, and the answer checked.
#    without agentdna:
#           client = httpx.Client(base_url=SUPPORT_AGENT_URL, timeout=300)
#           response = client.post("/request", json={"order": ORDER, "request": REQUEST})
client = SignedClient(lead, None, SUPPORT_AGENT_URL, timeout=300)
response = client.post("/request", json={"order": ORDER, "request": REQUEST})
print(f"\nSupport Agent: {response.json()}")

# 3. Record the whole task once, for the dashboard. Without agentdna: not needed
lead.record(client.workflow)
print(f"On the dashboard: workflow {client.workflow.id}")
