from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

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
    ] = field(
        default_factory=dict
    )

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
            raise RuntimeError(
                f"MCP call sequence {sequence} "
                "already has a recorded workflow"
            )

        self.completed_workflows[sequence] = workflow

        self.active_calls -= 1

        if self.active_calls < 0:
            raise RuntimeError(
                "MCP execution batch active-call count "
                "became negative"
            )

    @property
    def complete(self) -> bool:
        """
        True when every call belonging to this batch has completed.
        """

        return (
            self.active_calls == 0
            and self.next_sequence > 0
            and len(self.completed_workflows) == self.next_sequence
        )

    def terminal_frontier(
        self,
    ) -> list[IntentWorkflow]:
        """
        Return terminal workflows in call-start order.

        Completion order is deliberately NOT used.

        Example:

            call 0 starts
            call 1 starts

            call 1 finishes first
            call 0 finishes second

        Frontier remains:

            [result(call 0), result(call 1)]
        """

        if not self.complete:
            raise RuntimeError(
                "Cannot obtain terminal frontier "
                "before MCP execution batch completes"
            )

        return [
            self.completed_workflows[sequence]
            for sequence in range(
                self.next_sequence
            )
        ]


@dataclass(frozen=True)
class MCPCallHandle:
    """
    Immutable handle identifying one MCP call inside a batch.

    The HTTP interceptor keeps this handle across the await boundary.
    """

    batch: MCPExecutionBatch
    sequence: int


@dataclass
class AgentDNAContext:
    """
    Ambient AgentDNA execution context.

    `workflows` is the currently declared causal frontier.

    The MCP execution batch is managed automatically by the MCP
    client interceptor.

    AgentDNA does not infer anything about the Agent framework.
    """

    dna: AgentDNA

    workflows: list[IntentWorkflow]

    active_mcp_batch: MCPExecutionBatch | None = None

    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )

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
                raise RuntimeError(
                    "Cannot begin MCP call with "
                    "an empty causal frontier"
                )

            if self.active_mcp_batch is None:

                self.active_mcp_batch = (
                    MCPExecutionBatch(
                        parent_frontier=list(
                            self.workflows
                        )
                    )
                )

            batch = (
                self.active_mcp_batch
            )

            sequence = (
                batch.allocate_call()
            )

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

            current_batch = (
                self.active_mcp_batch
            )

            if current_batch is None:

                raise RuntimeError(
                    "Cannot complete MCP call: "
                    "no active MCP batch exists"
                )

            if current_batch is not handle.batch:

                raise RuntimeError(
                    "MCP call belongs to a stale "
                    "execution batch"
                )

            current_batch.record_completion(
                handle.sequence,
                workflow,
            )

            if not current_batch.complete:
                return

            new_frontier = (
                current_batch.terminal_frontier()
            )

            self.workflows = list(
                new_frontier
            )

            self.active_mcp_batch = None


    async def cancel_mcp_call(
        self,
        handle: MCPCallHandle,
    ) -> None:
        """
        Remove a failed MCP call from its active batch.

        This is used when the MCP transport itself fails before
        a server successor workflow is received.

        We deliberately do not invent a fake successor workflow.

        If other calls remain active, they continue using the same
        batch.

        If this was the last active call, the batch is discarded and
        the current frontier remains unchanged.
        """

        async with self.lock:

            current_batch = (
                self.active_mcp_batch
            )

            if current_batch is None:

                return

            if current_batch is not handle.batch:

                return

            if handle.sequence in (
                current_batch.completed_workflows
            ):
                return

            current_batch.active_calls -= 1

            if current_batch.active_calls == 0:
                self.active_mcp_batch = None


    def get_workflows(
        self,
    ) -> list[IntentWorkflow]:
        """
        Return a snapshot of the currently declared causal frontier.
        """

        return list(
            self.workflows
        )


_context: ContextVar[
    AgentDNAContext | None
] = ContextVar(
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
        initial_workflows = [
            workflows
        ]

    else:
        initial_workflows = list(
            workflows
        )

    if not initial_workflows:

        raise ValueError(
            "AgentDNA context requires at least "
            "one causal workflow"
        )

    context = AgentDNAContext(
        dna=dna,
        workflows=initial_workflows,
    )

    token = _context.set(
        context
    )

    try:
        yield context

    finally:
        _context.reset(
            token
        )


def get_workflows() -> list[IntentWorkflow]:
    """
    Return a snapshot of the current causal frontier.
    """

    context = get_context()

    if context is None:
        raise RuntimeError(
            "No active AgentDNA context"
        )

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
        raise RuntimeError(
            "No active AgentDNA context"
        )

    if isinstance(
        workflows,
        IntentWorkflow,
    ):
        new_workflows = [
            workflows
        ]

    else:
        new_workflows = list(
            workflows
        )

    if not new_workflows:

        raise ValueError(
            "AgentDNA context requires at least "
            "one causal workflow"
        )

    context.workflows = new_workflows