from __future__ import annotations

import argparse
import asyncio
import json
import sys

from config import settings
from swarm import describe, run_swarm

from agentdna import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the package due-diligence swarm on one request."
    )
    parser.add_argument("prompt", help="e.g. 'Should we adopt httpx 0.27.0 in production?'")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    # The user signs the request; the swarm verifies it before acting on it.
    run = await run_swarm(USER.build(payload=args.prompt))

    if args.json:
        print(json.dumps({"task": args.prompt, **describe(run)}), flush=True)
    else:
        print("ADK Package Swarm")
        print(f"Task: {args.prompt}")
        print(
            f"Hand-offs: {', '.join(f'{h.source} -> {h.target}' for h in run.handoffs) or 'none'}"
        )
        print(f"Tool calls: {', '.join(f'{a}: {t}' for a, t in run.tool_calls) or 'none'}")
        print(f"Answered by: {run.answered_by}\n")
        print(run.result.strip(), flush=True)

    # Commit the whole run's chain of custody to the Provenance Layer.
    try:
        USER.record(run.adna_workflow)
    except Exception as e:
        print(f"Error recording workflow: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
