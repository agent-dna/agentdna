from __future__ import annotations

import json
from typing import Any

from agents.sqlite_agent import SQLiteAnalyticsAgent
from config import settings

from agentdna.types import IntentWorkflow

def run_task(task_prompt: str, adna_workflow: IntentWorkflow) -> dict[str, Any]:
    return SQLiteAnalyticsAgent().run(task_prompt, adna_workflow)


def log_result(result: dict[str, Any], task_prompt: str) -> None:
    print(json.dumps({"event_type": "agent.execution.completed", "task": task_prompt, **result}), flush=True)


def print_human_result(result: dict[str, Any], task_prompt: str) -> None:
    print("SQLite Analytics Agent")
    print(f"Execution ID: {result['execution_id']}")
    print(f"Database: {result['database']}")
    print(f"Task: {task_prompt}")
    print()
    print(result["result"], flush=True)

