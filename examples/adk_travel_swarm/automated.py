from __future__ import annotations

import asyncio
import random
import time

from config import settings
from runner import log_result, run_task

from agentdna.core import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)

PROMPTS = (
    "I'm flying from New York to Lisbon this weekend. What's the weather, what is Lisbon known for, and how much is 500 USD in euros?",
    "Plan a short trip to Kyoto from London: forecast, local time, a quick overview, and 300 GBP in yen.",
    "Thinking of visiting Reykjavik from Toronto. How cold will it be, what is it known for, and what do 1000 CAD buy in Icelandic krona?",
    "Business trip from Sydney to Singapore: what's the weather and the local time there, and how much is 2000 AUD in Singapore dollars?",
    "Weekend in Prague from Berlin: forecast, what to know about the city, and 250 EUR in Czech koruna.",
)


async def main() -> None:
    prompt = random.choice(PROMPTS)
    run = await run_task(USER.build(payload=prompt))
    log_result(run, prompt)

    try:
        USER.record(run.adna_workflow)
    except Exception as e:
        print(f"Failed to record the workflow on the provenance layer: {e}")


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        time.sleep(3)
