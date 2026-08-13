from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agents.rss_orchestrator_agent import RSSOrchestratorAgent
from config import settings
from agentdna.types import IntentWorkflow

async def run_task(task_prompt: str, workflow: IntentWorkflow) -> dict[str, Any]:
    return await RSSOrchestratorAgent().run(task_prompt, workflow)


def log_result(result: dict[str, Any], task_prompt: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info(json.dumps({"event_type": "agent.execution.completed", "task": task_prompt, **result}))


def print_human_result(result: dict[str, Any], task_prompt: str) -> None:
    print("RSS Research Agent")
    print(f"Execution ID: {result['execution_id']}")
    print(f"Task: {task_prompt}\n")
    print("Worker Report 1 (Security Findings)")
    print(result["security_findings"].strip())
    print("\nWorker Report 2 (Technology Findings)")
    print(result["technology_findings"].strip())
    print("\nSynthesis")
    print(result["summary"].strip())

