"""
A run is one agent execution. The agent application opens it with the
workflow(s) it received, points its MCP client at the run's URL, and closes it
to collect the resulting frontier. Every tool call made through that URL
builds on, and advances, that run's frontier - and no other.
The run id is unguessable and is the only thing that ties an MCP request to a
run, so knowing it is what authorises a request to sign on that run's behalf.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from agentdna import AgentDNA
from agentdna.mcp.context import AgentDNAContext
from agentdna.types import IntentWorkflow


@dataclass(frozen=True)
class Termination:
    """Why a run's chain ended: a failure that was recorded on the provenance layer."""

    reason: str
    status_code: int | None = None


@dataclass
class Run:
    run_id: str
    context: AgentDNAContext
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    # Set once the chain has ended. A recorded IntentWorkflow is never passed
    # forward, so an ended run makes no more tool calls and returns no frontier.
    ended: Termination | None = None

    @property
    def calls_in_flight(self) -> int:
        batch = self.context.active_mcp_batch
        return batch.active_calls if batch is not None else 0

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def end(self, reason: str, status_code: int | None = None) -> None:
        """End the run's chain. The first reason given is kept."""

        if self.ended is None:
            self.ended = Termination(reason, status_code)


class RunRegistry:
    """In-memory runs, dropped once idle for longer than `ttl_seconds`.

    A run with a call in flight is never dropped, however long the call takes
    """

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def create(self, adna: AgentDNA, workflows: list[IntentWorkflow]) -> Run:
        if not workflows:
            raise ValueError("a run needs at least one workflow to build on")

        run = Run(
            run_id=secrets.token_urlsafe(24),
            context=AgentDNAContext(dna=adna, workflows=list(workflows)),
        )

        with self._lock:
            self._purge_expired()
            self._runs[run.run_id] = run

        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            self._purge_expired()
            run = self._runs.get(run_id)

        if run is not None:
            run.touch()

        return run

    def remove(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.pop(run_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._runs)

    def _purge_expired(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds

        expired = [
            run_id
            for run_id, run in self._runs.items()
            if run.last_used < cutoff and run.calls_in_flight == 0
        ]

        for run_id in expired:
            del self._runs[run_id]
