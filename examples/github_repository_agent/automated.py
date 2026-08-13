from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import log_result, run_task
from agentdna.core import AgentDNA
from config import settings

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url
)

PROMPTS = (
    "Summarise recent commits, open issues, and pull requests. Identify the most important delivery risks.",
    "Review open issues for stale work, repeated concerns, and blockers that need maintainer attention.",
    "Analyse open pull requests for scope, review risk, and dependencies on other changes.",
    "Inspect recent commits and identify components that changed frequently or may need follow-up testing.",
    "Review the repository metadata and top-level files. Identify documentation or ownership gaps.",
    "Find security-sensitive files or recent changes that deserve a focused code review by a maintainer.",
    "Identify unresolved issues that could affect the next release, grouping them by likely impact.",
    "Compare recent commits with open pull requests and report signals of stalled delivery work.",
    "Review repository activity for maintenance risk, including inactive pull requests and recurring issues.",
    "Create an evidence-based weekly engineering brief covering changes, issues, pull requests, and recommendations.",
)


async def main() -> None:
    prompt = random.choice(PROMPTS)

    adna_workflow = USER.build(
        payload=prompt
    )

    result = await run_task(prompt, adna_workflow)

    try:
        USER.record(result["adna_workflow"])
    except Exception as e:
        print(f"Failed to record the workflow on the provenance layer: {e}")


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        time.sleep(3)
    