"""What an agent uses to have its MCP tool calls signed.
    from agentdna.mcp.relay import ProvenanceRelay
    AGENT = AgentDNA(name="github-agent", type="agent", ...)
    PROVENANCE_RELAY = ProvenanceRelay({"github": "http://127.0.0.1:8011/mcp"})
    async with PROVENANCE_RELAY.session(AGENT, incoming_workflow) as session:
        client = MultiServerMCPClient(
            {"github": {"transport": "streamable_http", "url": session.url("github")}}
        )
        output = await agent.ainvoke(...)
    reply = AGENT.build(payload=str(output), previous_workflows=session.workflows)
`session()` verifies the incoming workflow with AGENT. Every tool call made
through `session.url(...)` - by any MCP client, unpatched - is signed as AGENT
and builds on it. Once the session is closed, `session.workflows` is the final
IntentWorkflow(s): the incoming workflow plus every tool call made through it.
`with` works the same way in synchronous code.

If an MCP server refuses a call - a whitelist, CoCA or CBAC check fails - it
records the refusal and that chain ends. The relay then refuses every further
tool call in the session, and closing it raises `ChainTerminated`: there are
no workflows to build on or pass forward.
The relay runs inside this process and starts on first use. It holds no
identity of its own, so one relay can serve every agent in the process,
each session signing as the agent that opened it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Iterator, Mapping
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from agentdna.types import IntentWorkflow

from .config import ProvenanceRelayConfig
from .runs import Run
from .service import RunEnded, RunError, mcp_path

if TYPE_CHECKING:
    from agentdna import AgentDNA

    from .embedded import EmbeddedRelay

T = TypeVar("T")


class RelayError(RuntimeError):
    """The relay refused or failed a request."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = body.get("error") if isinstance(body, dict) else body
        super().__init__(f"AgentDNA provenance relay refused the request ({status}): {message}")


class WorkflowRejected(RelayError):
    """The incoming workflow failed AgentDNA verification."""

    @property
    def status_code(self) -> int | None:
        return self.body.get("status_code") if isinstance(self.body, dict) else None


class ChainTerminated(RelayError):
    """The run's chain ended: an MCP server refused a tool call, or the relay
    could not verify an answer, and the failure was recorded on the provenance
    layer.

    A recorded IntentWorkflow is never passed forward, so the session has no
    workflows to return: do not build on the run, or pass it on. `reason` is
    the refusal as worded by whoever ended the chain.
    """

    @property
    def reason(self) -> str:
        return self.body.get("reason", "") if isinstance(self.body, dict) else ""

    @property
    def status_code(self) -> int | None:
        return self.body.get("status_code") if isinstance(self.body, dict) else None


class ProvenanceRelay:
    """Signs agents' MCP tool calls. Create one per process and reuse it."""

    def __init__(
        self,
        servers: Mapping[str, str],
        *,
        run_ttl_seconds: float = 3600.0,
        upstream_timeout_seconds: float = 300.0,
    ) -> None:
        """`servers` maps a name to each MCP server's Streamable HTTP URL."""

        self._config = ProvenanceRelayConfig(
            upstreams=dict(servers),
            run_ttl_seconds=run_ttl_seconds,
            upstream_timeout_seconds=upstream_timeout_seconds,
        )
        self._embedded: EmbeddedRelay | None = None
        self._lock = threading.Lock()

    def session(
        self,
        agent: AgentDNA,
        workflows: IntentWorkflow | list[IntentWorkflow],
    ) -> RelaySession:
        """A session for `agent` on the workflow(s) it received. Opens on entering `with`.
        The incoming workflow is verified with `agent`, and every tool call made
        through the session is signed as `agent`.
        """

        if isinstance(workflows, IntentWorkflow):
            workflows = [workflows]
        if not workflows:
            raise ValueError("a session needs at least one workflow to build on")
        return RelaySession(self, agent, list(workflows))

    def close(self) -> None:
        """Stop the relay, if it was started. Also runs at exit."""

        with self._lock:
            if self._embedded is not None:
                self._embedded.stop()
                self._embedded = None

    def _relay(self) -> EmbeddedRelay:
        with self._lock:
            if self._embedded is None:
                from .embedded import EmbeddedRelay

                # Starts the relay on first use. Blocking, but only once.
                self._embedded = EmbeddedRelay(self._config)
            return self._embedded


class RelaySession:
    """One agent execution on the relay. Usable with `with` or `async with`."""

    def __init__(
        self,
        relay: ProvenanceRelay,
        agent: AgentDNA,
        workflows: list[IntentWorkflow],
    ) -> None:
        self.relay = relay
        self.agent = agent
        self._workflows: list[IntentWorkflow] = workflows
        self.run_id = ""
        self.servers: list[str] = []
        self.closed = False
        self._terminated: ChainTerminated | None = None
        self._embedded: EmbeddedRelay | None = None
        self._mcp_base = ""

    def url(self, server: str | None = None) -> str:
        """The MCP URL to give the agent's MCP client for `server`.
        `server` may be left out when the relay fronts a single MCP server.
        """

        if not self.run_id:
            raise RuntimeError("the session is not open; use it inside `with`/`async with`")
        if server is None:
            if len(self.servers) != 1:
                raise ValueError(f"name the MCP server, one of {self.servers}")
            server = self.servers[0]
        if server not in self.servers:
            raise KeyError(f"unknown MCP server {server!r}; the relay fronts {self.servers}")
        return f"{self._mcp_base}/{server}"

    @property
    def workflows(self) -> list[IntentWorkflow]:
        """The final IntentWorkflow(s), once the session is closed.
        Build the agent's reply on these. While the session is open they are
        not final, so reading them raises instead of silently returning the
        incoming workflow; inside the block, `close()` / `await aclose()`
        returns them.
        """

        if self._terminated is not None:
            raise self._terminated
        if not self.closed:
            raise RuntimeError(
                "the session is still open: read `workflows` after the `with` block, "
                "or use the return value of close() / await aclose()"
            )
        return list(self._workflows)

    # -- sync ---------------------------------------------------------------

    def open(self) -> RelaySession:
        embedded = self._begin()
        self._opened(embedded, _wait(embedded, embedded.service.open(self.agent, self._workflows)))
        return self

    def frontier(self) -> list[IntentWorkflow]:
        """The workflows the tool calls have produced so far."""

        embedded = self._open_relay()
        return _wait(embedded, embedded.service.frontier(self.run_id)).context.get_workflows()

    def advance(self, workflows: IntentWorkflow | list[IntentWorkflow]) -> None:
        """Move the session's frontier forward to workflows the agent signed itself.
        For hops that happen outside MCP - a hand-off to another agent, say -
        so the session's next tool calls build on them. The workflows must
        build on the session's current frontier (`frontier()`); the chain can
        only be extended. Refused while tool calls are in flight.
        """

        embedded = self._open_relay()
        _wait(embedded, embedded.service.advance(self.run_id, _as_list(workflows)))

    def close(self, force: bool = False) -> list[IntentWorkflow]:
        """Close the session and return its final workflows. Idempotent.

        Raises `ChainTerminated` instead when the run's chain has ended.
        """

        if not self.closed:
            embedded = self._open_relay()
            with self._terminating():
                self._closed(_wait(embedded, embedded.service.close(self.run_id, force=force)))
        return self.workflows

    def __enter__(self) -> RelaySession:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.closed or not self.run_id:
            return
        if exc_type is None:
            self.close()
            return
        try:
            self.close(force=True)
        except Exception:
            pass  # do not mask the exception that ended the block

    # -- async --------------------------------------------------------------

    async def aopen(self) -> RelaySession:
        embedded = self._begin()
        run = await _await(embedded, embedded.service.open(self.agent, self._workflows))
        self._opened(embedded, run)
        return self

    async def afrontier(self) -> list[IntentWorkflow]:
        embedded = self._open_relay()
        run = await _await(embedded, embedded.service.frontier(self.run_id))
        return run.context.get_workflows()

    async def aadvance(self, workflows: IntentWorkflow | list[IntentWorkflow]) -> None:
        embedded = self._open_relay()
        await _await(embedded, embedded.service.advance(self.run_id, _as_list(workflows)))

    async def aclose(self, force: bool = False) -> list[IntentWorkflow]:
        if not self.closed:
            embedded = self._open_relay()
            with self._terminating():
                workflows = await _await(embedded, embedded.service.close(self.run_id, force=force))
                self._closed(workflows)
        return self.workflows

    async def __aenter__(self) -> RelaySession:
        return await self.aopen()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.closed or not self.run_id:
            return
        if exc_type is None:
            await self.aclose()
            return
        try:
            await self.aclose(force=True)
        except Exception:
            pass

    # -- shared -------------------------------------------------------------

    def _begin(self) -> EmbeddedRelay:
        if self.run_id:
            raise RuntimeError("the session is already open")
        return self.relay._relay()

    def _open_relay(self) -> EmbeddedRelay:
        if self._embedded is None or not self.run_id:
            raise RuntimeError("the session has not been opened")
        return self._embedded

    def _opened(self, embedded: EmbeddedRelay, run: Run) -> None:
        self._embedded = embedded
        self.run_id = run.run_id
        self.servers = embedded.service.servers
        self._mcp_base = f"{embedded.url}{mcp_path(run.run_id)}"

    def _closed(self, workflows: list[IntentWorkflow]) -> None:
        self._workflows = workflows
        self.closed = True

    @contextmanager
    def _terminating(self) -> Iterator[None]:
        """A close that finds the chain ended still closes the session."""

        try:
            yield
        except ChainTerminated as exc:
            self._terminated = exc
            self._closed([])
            raise


def _as_list(workflows: IntentWorkflow | list[IntentWorkflow]) -> list[IntentWorkflow]:
    return [workflows] if isinstance(workflows, IntentWorkflow) else list(workflows)


def _wait(embedded: EmbeddedRelay, coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a relay operation on the relay's loop and wait for it."""

    try:
        return embedded.submit(coroutine).result()
    except RunError as exc:
        raise _error(exc) from exc


async def _await(embedded: EmbeddedRelay, coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        return await asyncio.wrap_future(embedded.submit(coroutine))
    except RunError as exc:
        raise _error(exc) from exc


def _error(exc: RunError) -> RelayError:
    body = {"error": str(exc), **exc.details}
    if isinstance(exc, RunEnded):
        return ChainTerminated(exc.status, body)
    if exc.status == 422:
        return WorkflowRejected(exc.status, body)
    return RelayError(exc.status, body)
