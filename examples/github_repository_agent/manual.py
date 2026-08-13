from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings
from runner import log_result, print_human_result, run_task
from agentdna.core import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one GitHub repository analysis with a supplied prompt.")
    parser.add_argument("prompt", help="Analysis task for the configured repository.")
    parser.add_argument("--json", action="store_true", help="Emit the structured execution event as JSON.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    try:
        adna_workflow = USER.build(
            payload=args.prompt
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
        
    result = await run_task(args.prompt, adna_workflow)

    if args.json:
        log_result(result, args.prompt)
    else:
        print_human_result(result, args.prompt)

    try:
        USER.record(result["adna_workflow"])
    except Exception as e:
        print(f"Error recording workflow: {e}", file=sys.stderr)
        sys.exit(1)
    
if __name__ == "__main__":
    asyncio.run(main())