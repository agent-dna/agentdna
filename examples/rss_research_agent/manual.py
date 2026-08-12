from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import log_result, print_human_result, run_task
from agentdna.core import AgentDNA
from agentdna.types import IntentWorkflow
from config import settings

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one RSS research workflow with a supplied prompt.")
    parser.add_argument("prompt", help="Research task for the configured RSS feeds.")
    parser.add_argument("--json", action="store_true", help="Print the structured automation event instead of the readable report.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    prompt = args.prompt
    try:
        agentdna_workflow = USER.build(
            payload=prompt,
        )

        result = await run_task(prompt, agentdna_workflow)

        # Get the ADNA workflow and record it on provenance layer
        result_workflow: IntentWorkflow = result["adna_workflow"]
        if not result_workflow:
            raise ValueError("ADNA workflow is missing in the result.")

        try:
            USER.record(result_workflow)
        except Exception as e:
            print(f"Failed to record the workflow on the provenance layer: {e}")

        if args.json:
            log_result(result, prompt)
        else:
            print_human_result(result, prompt)

        
    except Exception as error:
        raise


if __name__ == "__main__":
    asyncio.run(main())