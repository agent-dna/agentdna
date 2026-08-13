from __future__ import annotations

import random
import sys
from pathlib import Path
import time

from config import settings

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import log_result, run_task

from agentdna.core import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)


PROMPTS = (
    "Analyse monthly revenue and order volume for notable changes and potential causes.",
    "Identify the products with the greatest revenue and quantity sold, grouped by category.",
    "Find pending orders and assess whether order status patterns require operational follow-up.",
    "Compare customer purchasing activity and identify customers with unusually high order value.",
    "Review product categories for uneven sales performance and recommend where to investigate further.",
    "Analyse recent orders for unusual quantities, prices, or order status combinations.",
    "Create an evidence-based weekly business briefing using order, customer, and product data.",
    "Find the most valuable customer-product relationships and identify concentration risk.",
    "Look for business trends that could indicate growing demand or a drop in completed orders.",
    "Assess the database for analytics findings that need a manager's attention, including anomalies and opportunities.",
)


def main() -> None:
    prompt = random.choice(PROMPTS)

    adna_workflow = USER.build(
        payload=prompt,
    )       

    result = run_task(prompt, adna_workflow)

    try:
        USER.record(result["adna_workflow"])
    except Exception as e:
        print(f"Failed to record the workflow on the provenance layer: {e}")


if __name__ == "__main__":
    while True:
        main()
        time.sleep(3)