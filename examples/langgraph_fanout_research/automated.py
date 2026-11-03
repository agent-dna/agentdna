from __future__ import annotations

import asyncio
import random
import time

from runner import log_result, run_task

PROMPTS = (
    "Compare Kenya, Germany and India on renewable energy share and internet access over the last decade, and explain what drives the differences.",
    "How have electricity access and GDP per capita changed in Nigeria, Bangladesh and Vietnam since 2015?",
    "Brazil, Indonesia and Mexico: population growth since 2015, internet adoption, and one notable fact about each country's economy.",
    "Which of Norway, Iceland and Costa Rica gets the largest share of its energy from renewables, and why?",
    "Compare the digital divide in Ethiopia and Rwanda: internet users and electricity access over the last decade.",
)


async def main() -> None:
    prompt = random.choice(PROMPTS)
    run = await run_task(prompt)
    log_result(run, prompt)


if __name__ == "__main__":
    while True:
        asyncio.run(main())
        time.sleep(3)
