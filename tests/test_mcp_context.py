"""Tests for the causal frontier kept by AgentDNAContext.

The frontier is what the next MCP call builds on, and what the agent builds
its own reply on. Two of these guard the cases that used to lose provenance:

    test_cancel_before_success_does_not_wedge_the_batch
    test_cancel_after_success_keeps_the_successful_branch
"""

import asyncio

import pytest

from agentdna.mcp.context import AgentDNAContext
from agentdna.types import IntentWorkflow


def wf(name: str) -> IntentWorkflow:
    return IntentWorkflow(id=name, type="intent_workflow", version="2.0")


def ids(workflows):
    return [workflow.id for workflow in workflows]


def new_context(*names: str) -> AgentDNAContext:
    return AgentDNAContext(dna=None, workflows=[wf(name) for name in names or ("root",)])  # type: ignore[arg-type]


def run(coro):
    return asyncio.run(coro)


def test_sequential_calls_chain():
    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        await ctx.complete_mcp_call(a, wf("A"))
        b = await ctx.begin_mcp_call()
        assert ids(b.batch.parent_frontier) == ["A"]
        await ctx.complete_mcp_call(b, wf("B"))
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["B"]
    assert ctx.active_mcp_batch is None


def test_overlapping_calls_share_a_parent_and_merge_in_start_order():
    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        assert a.batch is b.batch
        # Completion order is the reverse of start order.
        await ctx.complete_mcp_call(b, wf("B"))
        assert ids(ctx.workflows) == ["root"]
        await ctx.complete_mcp_call(a, wf("A"))
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["A", "B"]
    assert ctx.active_mcp_batch is None


def test_cancel_before_success_does_not_wedge_the_batch():
    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        await ctx.cancel_mcp_call(b)
        await ctx.complete_mcp_call(a, wf("A"))
        assert ctx.active_mcp_batch is None
        assert ids(ctx.workflows) == ["A"]

        c = await ctx.begin_mcp_call()
        assert ids(c.batch.parent_frontier) == ["A"]
        await ctx.complete_mcp_call(c, wf("C"))
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["C"]


def test_cancel_after_success_keeps_the_successful_branch():
    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        await ctx.complete_mcp_call(a, wf("A"))
        await ctx.cancel_mcp_call(b)
        assert ctx.active_mcp_batch is None
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["A"]


def test_all_cancelled_keeps_the_starting_frontier():
    async def scenario():
        ctx = new_context("root-1", "root-2")
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        await ctx.cancel_mcp_call(a)
        await ctx.cancel_mcp_call(b)
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["root-1", "root-2"]
    assert ctx.active_mcp_batch is None


def test_repeated_or_late_cancel_is_ignored():
    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        await ctx.cancel_mcp_call(b)
        await ctx.cancel_mcp_call(b)  # repeated: must not count twice
        assert ctx.active_mcp_batch is not None
        await ctx.complete_mcp_call(a, wf("A"))
        await ctx.cancel_mcp_call(a)  # late: batch already closed
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["A"]
    assert ctx.active_mcp_batch is None


def test_completing_a_cancelled_call_is_refused():
    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        await ctx.cancel_mcp_call(a)
        with pytest.raises(RuntimeError):
            await ctx.complete_mcp_call(a, wf("A"))
        await ctx.complete_mcp_call(b, wf("B"))
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["B"]


def test_call_joining_a_running_batch_after_a_cancel():
    """A call that starts while the batch is still open joins it."""

    async def scenario():
        ctx = new_context()
        a = await ctx.begin_mcp_call()
        b = await ctx.begin_mcp_call()
        await ctx.cancel_mcp_call(b)
        c = await ctx.begin_mcp_call()
        assert c.batch is a.batch
        await ctx.complete_mcp_call(c, wf("C"))
        assert ctx.active_mcp_batch is not None
        await ctx.complete_mcp_call(a, wf("A"))
        return ctx

    ctx = run(scenario())
    assert ids(ctx.workflows) == ["A", "C"]
