from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from agentdna import AgentDNA
from agentdna.types import IntentWorkflow


@dataclass
class MCPExecutionBatch:
    """
    Represents one observable batch of overlapping MCP calls.

    AgentDNA deliberately does NOT call this an Agent interaction.

    The batch only represents something we can actually observe:

        MCP calls that were active concurrently.

    All calls in one batch use the same parent frontier snapshot.
    """

    parent_frontier: list[IntentWorkflow]

    next_sequence: int = 0

    active_calls: int = 0

    completed_workflows: dict[
        int,
        IntentWorkflow,
    ] = field(default_factory=dict)

    # Calls that ended without a successor workflow. They still count
    # towards completing the batch, but contribute nothing to its frontier.
    cancelled_calls: set[int] = field(default_factory=set)

    def allocate_call(
        self,
    ) -> int:
        """
        Allocate a deterministic sequence number for a new MCP call.
        """

        sequence = self.next_sequence

        self.next_sequence += 1
        self.active_calls += 1

        return sequence

    def record_completion(
        self,
        sequence: int,
        workflow: IntentWorkflow,
    ) -> None:
        """
        Record the terminal workflow for one MCP call.
        """

        if sequence in self.completed_workflows:
            raise RuntimeError(f"MCP call sequence {sequence} already has a recorded workflow")

        if sequence in self.cancelled_calls:
            raise RuntimeError(f"MCP call sequence {sequence} was already cancelled")

        self.completed_workflows[sequence] = workflow

        self.active_calls -= 1

        if self.active_calls < 0:
            raise RuntimeError("MCP execution batch active-call count became negative")

    def record_cancellation(
        self,
        sequence: int,
    ) -> bool:
        """
        Record that one MCP call ended without a successor workflow.

        Returns False when the call was already resolved, so a late or
        repeated cancellation never disturbs the batch.
        """

        if sequence in self.completed_workflows or sequence in self.cancelled_calls:
            return False

        self.cancelled_calls.add(sequence)

        self.active_calls -= 1

        if self.active_calls < 0:
            raise RuntimeError("MCP execution batch active-call count became negative")

        return True

    @property
    def complete(
        self,
    ) -> bool:
        """
        True when every call belonging to this batch has either
        completed or been cancelled.
        """

        return (
            self.active_calls == 0
            and self.next_sequence > 0
            and len(self.completed_workflows) + len(self.cancelled_calls) == self.next_sequence
        )

    def terminal_frontier(
        self,
    ) -> list[IntentWorkflow]:
        """
        Return terminal workflows in call-start order.

        Completion order is deliberately NOT used. Cancelled calls have
        no successor and are skipped, so the list is empty when every
        call in the batch was cancelled.
        """

        if not self.complete:
            raise RuntimeError(
                "Cannot obtain terminal frontier before MCP execution batch completes"
            )

        return [
            self.completed_workflows[sequence]
            for sequence in range(self.next_sequence)
            if sequence in self.completed_workflows
        ]


@dataclass(frozen=True)
class MCPCallHandle:
    """
    Immutable handle identifying one MCP call inside a batch.
    """

    batch: MCPExecutionBatch

    sequence: int


@dataclass
class AgentDNAContext:
    """
    Ambient AgentDNA execution context.

    `workflows` is the currently declared causal frontier.

    MCP execution batches are managed automatically by the MCP
    client integration.
    """

    dna: AgentDNA

    workflows: list[IntentWorkflow]

    active_mcp_batch: MCPExecutionBatch | None = None

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # MCP request metadata currently associated with the
    # active logical MCP call.
    #
    # This is transport-neutral. The official MCP client
    # integration converts it into MCP `params._meta`.
    mcp_request_meta: dict[str, Any] | None = None

    async def begin_mcp_call(
        self,
    ) -> MCPCallHandle:
        """
        Begin one observable MCP call.

        If a batch is already active, this call joins it.

        Otherwise, a new batch is created using a snapshot of the
        current causal frontier.
        """

        async with self.lock:
            if not self.workflows:
                raise RuntimeError("Cannot begin MCP call with an empty causal frontier")

            if self.active_mcp_batch is None:
                self.active_mcp_batch = MCPExecutionBatch(parent_frontier=list(self.workflows))

            batch = self.active_mcp_batch

            sequence = batch.allocate_call()

            return MCPCallHandle(
                batch=batch,
                sequence=sequence,
            )

    async def complete_mcp_call(
        self,
        handle: MCPCallHandle,
        workflow: IntentWorkflow,
    ) -> None:
        """
        Record one MCP successor.

        When the final active call in the batch completes, the batch
        becomes the new causal frontier.
        """

        async with self.lock:
            current_batch = self.active_mcp_batch

            if current_batch is None:
                raise RuntimeError("Cannot complete MCP call: no active MCP batch exists")

            if current_batch is not handle.batch:
                raise RuntimeError("MCP call belongs to a stale execution batch")

            current_batch.record_completion(
                handle.sequence,
                workflow,
            )

            self._close_batch_if_complete()

    async def cancel_mcp_call(
        self,
        handle: MCPCallHandle,
    ) -> None:
        """
        Resolve a failed MCP call without a successor workflow.

        No fake successor workflow is created. Successors already
        recorded by other calls in the same batch are kept.
        """

        async with self.lock:
            current_batch = self.active_mcp_batch

            if current_batch is None:
                return

            if current_batch is not handle.batch:
                return

            if not current_batch.record_cancellation(handle.sequence):
                return

            self._close_batch_if_complete()

    def _close_batch_if_complete(
        self,
    ) -> None:
        """
        Once every call in the active batch is resolved, its successful
        successors become the new causal frontier.

        If every call was cancelled there is nothing to advance to, and
        the frontier the batch started from stays in place.

        Must be called with `lock` held.
        """

        current_batch = self.active_mcp_batch

        if current_batch is None or not current_batch.complete:
            return

        new_frontier = current_batch.terminal_frontier()

        if new_frontier:
            self.workflows = list(new_frontier)

        self.active_mcp_batch = None

    def get_workflows(
        self,
    ) -> list[IntentWorkflow]:
        """
        Return a snapshot of the currently declared causal frontier.
        """

        return list(self.workflows)


_context: ContextVar[AgentDNAContext | None] = ContextVar(
    "agentdna_context",
    default=None,
)


def get_context() -> AgentDNAContext | None:
    return _context.get()


@contextmanager
def agentdna_context(
    dna: AgentDNA,
    workflows: IntentWorkflow | list[IntentWorkflow],
) -> Iterator[AgentDNAContext]:
    """
    Establish an AgentDNA execution context.
    """

    if isinstance(
        workflows,
        IntentWorkflow,
    ):
        initial_workflows = [workflows]

    else:
        initial_workflows = list(workflows)

    if not initial_workflows:
        raise ValueError("AgentDNA context requires at least one causal workflow")

    context = AgentDNAContext(
        dna=dna,
        workflows=initial_workflows,
    )

    token = _context.set(context)

    try:
        yield context

    finally:
        _context.reset(token)


def get_workflows() -> list[IntentWorkflow]:
    """
    Return a snapshot of the current causal frontier.
    """

    context = get_context()

    if context is None:
        raise RuntimeError("No active AgentDNA context")

    return context.get_workflows()


def set_workflows(
    workflows: IntentWorkflow | list[IntentWorkflow],
) -> None:
    """
    Explicitly replace the current causal frontier.

    This is application-controlled.

    MCP execution automatically updates the frontier when an
    MCP execution batch completes.
    """

    context = get_context()

    if context is None:
        raise RuntimeError("No active AgentDNA context")

    if isinstance(
        workflows,
        IntentWorkflow,
    ):
        new_workflows = [workflows]

    else:
        new_workflows = list(workflows)

    if not new_workflows:
        raise ValueError("AgentDNA context requires at least one causal workflow")

    context.workflows = new_workflows
