from __future__ import annotations

import asyncio
import json
import random
import time

from config import settings
from swarm import describe, run_swarm

from agentdna import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)

PROMPTS = (
    "We're about to pin requests 2.31.0 in production. Is that safe, and should we upgrade?",
    "Due diligence on httpx: is the latest version safe to adopt, and how healthy is the project?",
    "Compare flask and fastapi for a new internal service: maintenance, vulnerabilities and dependencies.",
    "Is pyyaml 5.3 still OK to use? What would we need to upgrade to?",
    "Our team wants to add pydantic. How many dependencies does it pull in, and is it well maintained?",
)


async def main() -> None:
    prompt = random.choice(PROMPTS)
    run = await run_swarm(USER.build(payload=prompt))
    print(json.dumps({"task": prompt, **describe(run)}), flush=True)

    try:
        USER.record(run.adna_workflow)
    except Exception as e:
        print(f"Failed to record the workflow on the provenance layer: {e}")


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        time.sleep(3)
