from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from unittest import result
import time

from agentdna.core import AgentDNA
from agentdna.types import IntentWorkflow

from config import settings

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import log_result, run_task
    
USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url
)

PROMPTS = (
    "Identify the most important security and technology developments from the configured feeds and explain their cross-domain implications.",
    "Create a weekly briefing on vulnerabilities, identity security, and developer platform changes.",
    "Research AI and cloud developments that introduce a new security consideration for engineering teams.",
    "Find material software supply-chain, authentication, and infrastructure developments. Separate facts from interpretation.",
    "Identify stories relevant to secure software delivery, including technology changes that require security review.",
    "Summarise developments in cybersecurity and developer tooling that could affect a platform engineering roadmap.",
    "Research recent AI-security, cloud-security, and database-platform stories, with clear worker provenance.",
    "Find recurring themes across the configured feeds involving vulnerabilities, developer platforms, and infrastructure.",
    "Create a concise research brief for security and technology leaders, highlighting major changes and open questions.",
    "Assess the configured RSS feeds for developments with likely impact on application security, engineering productivity, or cloud operations.",
)


async def main() -> None:
    print("Starting RSS research agent...")
    prompt = random.choice(PROMPTS)

    adna_workflow = USER.build(
        payload=prompt,
    )

    result = await run_task(prompt, adna_workflow)

    result_workflow: IntentWorkflow = result["adna_workflow"]
    if not result_workflow:
        raise ValueError("ADNA workflow is missing in the result.")

    try:
        USER.record(result_workflow)
    except Exception as e:
        print(f"Failed to record the workflow on the provenance layer: {e}")

    time.sleep(3)

if __name__ == "__main__":
    while True:
        asyncio.run(main())