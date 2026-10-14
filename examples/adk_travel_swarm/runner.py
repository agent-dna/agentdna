from __future__ import annotations

import json
from typing import Any

from swarm import SwarmRun, describe, run_swarm

from agentdna.types import IntentWorkflow


async def run_task(adna_workflow: IntentWorkflow) -> SwarmRun:
    return await run_swarm(adna_workflow)


def log_result(run: SwarmRun, task_prompt: str) -> None:
    event: dict[str, Any] = {
        "event_type": "agent.execution.completed",
        "task": task_prompt,
        **describe(run),
    }
    print(json.dumps(event), flush=True)


def print_human_result(run: SwarmRun, task_prompt: str) -> None:
    print("ADK Travel Swarm")
    print(f"Task: {task_prompt}")
    print(f"Hand-offs: {', '.join(f'{h.source} -> {h.target}' for h in run.handoffs) or 'none'}")
    print(f"Answered by: {run.answered_by}\n")
    print(run.result.strip(), flush=True)
