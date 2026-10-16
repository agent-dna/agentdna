"""Opening, reading and closing runs.
Sessions call these directly - there is no endpoint for them, so nothing else
can open a run and have tool calls signed. They run on the relay's event
loop, so a close is atomic with respect to the tool calls the same loop is
processing.
The relay holds no identity of its own: each run signs and verifies with
the agent that opened it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentdna import AgentDNA
from agentdna.error import RESULT_OK
from agentdna.log import get_logger
from agentdna.types import IntentWorkflow

from .config import ProvenanceRelayConfig
from .runs import Run, RunRegistry

logger = get_logger("agentdna.relay")


class RunError(Exception):
    """A run operation was refused. `status` is the matching HTTP status."""

    status = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class RunNotFound(RunError):
    status = 404


class CallsInFlight(RunError):
    status = 409


class WorkflowFailedVerification(RunError):
    status = 422


class VerificationUnavailable(RunError):
    status = 503


class NotAContinuation(RunError):
    status = 400


class RunEnded(RunError):
    """The run's chain ended: a failure was recorded, so it has no frontier to return."""

    status = 410


def mcp_path(run_id: str) -> str:
    """Where a run's MCP servers are reached: `{mcp_path}/{server name}`."""
    return f"/runs/{run_id}/mcp"


class RunService:
    def __init__(self, config: ProvenanceRelayConfig, runs: RunRegistry) -> None:
        self.config = config
        self.runs = runs

    @property
    def servers(self) -> list[str]:
        return sorted(self.config.upstreams)

    async def open(self, agent: AgentDNA, workflows: list[IntentWorkflow]) -> Run:
        """Verify the workflow(s) `agent` received and open a run on them.
        Tool calls made in the run are signed as `agent`.
        """

        for workflow in workflows:
            try:
                code = await asyncio.to_thread(agent.verify, workflow)
            except Exception as exc:
                logger.error("relay.run.verify_error", error=str(exc))
                raise VerificationUnavailable(f"unable to verify the workflow: {exc}")
            if code != RESULT_OK:
                raise WorkflowFailedVerification(
                    "the workflow failed AgentDNA verification", status_code=code
                )

        run = self.runs.create(agent, workflows)
        logger.info("agentdna.relay.run.opened", run_id=run.run_id, workflows=len(workflows))
        return run

    async def frontier(self, run_id: str) -> Run:
        return self._get(run_id)

    async def advance(self, run_id: str, workflows: list[IntentWorkflow]) -> list[IntentWorkflow]:
        """Move the run's frontier forward to `workflows`, signed by the app itself.
        For hops that happen outside MCP - a hand-off to another agent, say -
        so the run's next tool calls build on them. The new workflows must
        contain every workflow on the current frontier: the chain can only be
        extended, never cut. Refused while tool calls are in flight, whose
        results would otherwise land on a frontier they were not built for.
        """

        run = self._get(run_id)
        _refuse_if_ended(run)
        if not workflows:
            raise NotAContinuation("advance needs at least one workflow")
        if run.calls_in_flight:
            raise CallsInFlight(
                f"{run.calls_in_flight} tool call(s) still in flight; advance after they finish"
            )

        reachable = {
            (envelope.hash, envelope.signature)
            for workflow in workflows
            for envelope in workflow.unwrap()
        }
        current = run.context.get_workflows()
        for workflow in current:
            tip = workflow.get_latest_envelope()
            if (tip.hash, tip.signature) not in reachable:
                raise NotAContinuation(
                    "the new workflows do not build on the run's current frontier"
                )

        async with run.context.lock:
            run.context.workflows = list(workflows)
        logger.info("relay.run.advanced", run_id=run_id, workflows=len(workflows))
        return run.context.get_workflows()

    async def close(self, run_id: str, force: bool = False) -> list[IntentWorkflow]:
        """Close the run and return its final frontier."""

        run = self._get(run_id)
        if run.ended is not None:
            self.runs.remove(run_id)
            logger.info("relay.run.closed", run_id=run_id, forced=force, ended=True)
            _refuse_if_ended(run)
        if run.calls_in_flight and not force:
            raise CallsInFlight(
                f"{run.calls_in_flight} tool call(s) still in flight; "
                "wait for them or close with force"
            )

        self.runs.remove(run_id)
        logger.info("relay.run.closed", run_id=run_id, forced=force)
        return run.context.get_workflows()

    def _get(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if run is None:
            raise RunNotFound("unknown or expired run")
        return run


def _refuse_if_ended(run: Run) -> None:
    if run.ended is not None:
        raise RunEnded(
            f"the run's chain has ended: {run.ended.reason}",
            reason=run.ended.reason,
            status_code=run.ended.status_code,
        )
