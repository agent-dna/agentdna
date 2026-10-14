from __future__ import annotations

import argparse
import asyncio
import sys

from config import settings
from runner import log_result, print_human_result, run_task

from agentdna.core import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the travel research swarm on one request.")
    parser.add_argument("prompt", help="The travel request, e.g. 'Weekend in Lisbon from the US'.")
    parser.add_argument(
        "--json", action="store_true", help="Print the structured execution event as JSON."
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    adna_workflow = USER.build(payload=args.prompt)
    run = await run_task(adna_workflow)

    if args.json:
        log_result(run, args.prompt)
    else:
        print_human_result(run, args.prompt)

    try:
        USER.record(run.adna_workflow)
    except Exception as e:
        print(f"Error recording workflow: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
