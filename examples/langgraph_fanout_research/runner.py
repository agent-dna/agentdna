from __future__ import annotations

import json
from typing import Any

from config import settings
from graph import FanoutRun, describe, run_fanout

from agentdna import AgentDNA
from agentdna.types import IntentWorkflow

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)


async def run_task(question: str) -> FanoutRun:
    """The user signs the question, the fan-out runs, and the user records the chain."""

    run = await run_fanout(USER.build(payload=question))
    record(run)
    return run


def record(run: FanoutRun) -> None:
    # Commit the run's chain of custody to the Provenance Layer.
    try:
        USER.record(run.adna_workflow)
    except Exception as e:
        print(f"Failed to record the workflow on the provenance layer: {e}", flush=True)


def adna_summary(workflow: IntentWorkflow) -> dict[str, str]:
    """The signed answer's intent ID and final envelope, for the JSON event."""
    return {"id": workflow.id, "tip": workflow.get_latest_envelope().hash}


def log_result(run: FanoutRun, task_prompt: str) -> None:
    event: dict[str, Any] = {
        "event_type": "agent.execution.completed",
        "task": task_prompt,
        **describe(run),
        "adna_workflow": adna_summary(run.adna_workflow),
    }
    print(json.dumps(event), flush=True)


def print_human_result(run: FanoutRun, task_prompt: str) -> None:
    print("LangGraph Fan-out Research")
    print(f"Task: {task_prompt}")
    print(f"Workers spawned: {len(run.reports)}")
    for report in run.reports:
        print(f"  {report.instance}: {report.title} ({'ok' if report.ok else 'failed'})")
    print()
    print(run.result.strip(), flush=True)
