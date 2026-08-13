from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner import log_result, print_human_result, run_task
from config import settings

from agentdna.core import AgentDNA

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one SQLite analysis with a supplied prompt.")
    parser.add_argument("prompt", help="Read-only analysis task for the configured database.")
    parser.add_argument("--json", action="store_true", help="Print the completed execution as JSON instead of a human-readable report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        adna_workflow = USER.build(
            payload=args.prompt,
        )

        result = run_task(args.prompt, adna_workflow)

        USER.record(result["adna_workflow"])

    except Exception as e:
        print(f"Error running task: {e}")
        sys.exit(1)

    if args.json:
        log_result(result, args.prompt)
    else:
        print_human_result(result, args.prompt)


if __name__ == "__main__":
    main()