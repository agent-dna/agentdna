from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents.github_agent import GitHubRepositoryAgent
from config import settings


async def run_task(task_prompt: str, adna_workflow) -> dict[str, Any]:
    return await GitHubRepositoryAgent().run(task_prompt, adna_workflow)


def log_result(result: dict[str, Any], task_prompt: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info(json.dumps({"event_type": "agent.execution.completed", "task": task_prompt, **result}))


def print_human_result(result: dict[str, Any], task_prompt: str) -> None:
    print("GitHub Repository Agent")
    print(f"Execution ID: {result['execution_id']}")
    print(f"Repository: {result['repository']}")
    print(f"Task: {task_prompt}\n")
    print("Analysis")
    print(result["result"].strip())

