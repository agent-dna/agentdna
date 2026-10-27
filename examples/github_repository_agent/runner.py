from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents.github_agent import GitHubRepositoryAgent
from config import settings
from agentdna.types import IntentWorkflow


async def run_task(adna_workflow: IntentWorkflow) -> dict[str, Any]:
    return await GitHubRepositoryAgent().run(adna_workflow)


def log_result(result: dict[str, Any], task_prompt: str) -> None:
    event = {"event_type": "agent.execution.completed", "task": task_prompt, **result}
    # The signed IntentWorkflow isn't JSON itself: identify it by intent and final envelope.
    workflow = result["adna_workflow"]
    event["adna_workflow"] = {"id": workflow.id, "tip": workflow.get_latest_envelope().hash}
    print(json.dumps(event), flush=True)


def print_human_result(result: dict[str, Any], task_prompt: str) -> None:
    print("GitHub Repository Agent")
    print(f"Execution ID: {result['execution_id']}")
    print(f"Repository: {result['repository']}")
    print(f"Task: {task_prompt}\n")
    print("Analysis")
    print(result["result"].strip())

