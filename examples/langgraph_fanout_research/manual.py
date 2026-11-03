from __future__ import annotations

import argparse
import asyncio

from runner import log_result, print_human_result, run_task


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fan-out research agents on one question.")
    parser.add_argument("prompt", help="The research question.")
    parser.add_argument(
        "--json", action="store_true", help="Print the structured execution event as JSON."
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    run = await run_task(args.prompt)

    if args.json:
        log_result(run, args.prompt)
    else:
        print_human_result(run, args.prompt)


if __name__ == "__main__":
    asyncio.run(main())
