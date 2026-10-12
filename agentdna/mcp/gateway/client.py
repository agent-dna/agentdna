"""What an agent uses to have its MCP tool calls signed.

    from agentdna.mcp.gateway import MCPGateway

    AGENT = AgentDNA(name="github-agent", type="agent", ...)
    MCP_GATEWAY = MCPGateway({"github": "http://127.0.0.1:8011/mcp"})

    async with MCP_GATEWAY.session(AGENT, incoming_workflow) as session:
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

The gateway runs inside this process and starts on first use. It holds no
identity of its own, so one gateway can serve every agent in the process,
each session signing as the agent that opened it.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from agentdna.types import IntentWorkflow

from .config import GatewayConfig
from .runs import Run
from .service import RunError, mcp_path

if TYPE_CHECKING:
    from agentdna import AgentDNA

    from .embedded import EmbeddedGateway

T = TypeVar("T")


class GatewayError(RuntimeError):
    """The gateway refused or failed a request."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = body.get("error") if isinstance(body, dict) else body
        super().__init__(f"AgentDNA MCP gateway refused the request ({status}): {message}")


class WorkflowRejected(GatewayError):
    """The incoming workflow failed AgentDNA verification."""

    @property
    def status_code(self) -> int | None:
        return self.body.get("status_code") if isinstance(self.body, dict) else None


class MCPGateway:
    """Signs agents' MCP tool calls. Create one per process and reuse it."""

    def __init__(
        self,
        servers: Mapping[str, str],
        *,
        run_ttl_seconds: float = 3600.0,
        upstream_timeout_seconds: float = 300.0,
    ) -> None:
        """`servers` maps a name to each MCP server's Streamable HTTP URL."""

        self._config = GatewayConfig(
            upstreams=dict(servers),
            run_ttl_seconds=run_ttl_seconds,
            upstream_timeout_seconds=upstream_timeout_seconds,
        )
        self._embedded: EmbeddedGateway | None = None
        self._lock = threading.Lock()

    def session(
        self,
        agent: AgentDNA,
        workflows: IntentWorkflow | list[IntentWorkflow],
    ) -> GatewaySession:
        """A session for `agent` on the workflow(s) it received. Opens on entering `with`.

        The incoming workflow is verified with `agent`, and every tool call made
        through the session is signed as `agent`.
        """

        if isinstance(workflows, IntentWorkflow):
            workflows = [workflows]
        if not workflows:
            raise ValueError("a session needs at least one workflow to build on")
        return GatewaySession(self, agent, list(workflows))

    def close(self) -> None:
        """Stop the gateway, if it was started. Also runs at exit."""

        with self._lock:
            if self._embedded is not None:
                self._embedded.stop()
                self._embedded = None

    def _gateway(self) -> EmbeddedGateway:
        with self._lock:
            if self._embedded is None:
                from .embedded import EmbeddedGateway

                # Starts the gateway on first use. Blocking, but only once.
                self._embedded = EmbeddedGateway(self._config)
            return self._embedded


class GatewaySession:
    """One agent execution on the gateway. Usable with `with` or `async with`."""

    def __init__(
        self,
        gateway: MCPGateway,
        agent: AgentDNA,
        workflows: list[IntentWorkflow],
    ) -> None:
        self.gateway = gateway
        self.agent = agent
        self._workflows: list[IntentWorkflow] = workflows
        self.run_id = ""
        self.servers: list[str] = []
        self.closed = False
        self._embedded: EmbeddedGateway | None = None
        self._mcp_base = ""

    def url(self, server: str | None = None) -> str:
        """The MCP URL to give the agent's MCP client for `server`.

        `server` may be left out when the gateway fronts a single MCP server.
        """

        if not self.run_id:
            raise RuntimeError("the session is not open; use it inside `with`/`async with`")
        if server is None:
            if len(self.servers) != 1:
                raise ValueError(f"name the MCP server, one of {self.servers}")
            server = self.servers[0]
        if server not in self.servers:
            raise KeyError(f"unknown MCP server {server!r}; the gateway fronts {self.servers}")
        return f"{self._mcp_base}/{server}"

    @property
    def workflows(self) -> list[IntentWorkflow]:
        """The final IntentWorkflow(s), once the session is closed.

        Build the agent's reply on these. While the session is open they are
        not final, so reading them raises instead of silently returning the
        incoming workflow; inside the block, `close()` / `await aclose()`
        returns them.
        """

        if not self.closed:
            raise RuntimeError(
                "the session is still open: read `workflows` after the `with` block, "
                "or use the return value of close() / await aclose()"
            )
        return list(self._workflows)

    # -- sync ---------------------------------------------------------------

    def open(self) -> GatewaySession:
        embedded = self._begin()
        self._opened(embedded, _wait(embedded, embedded.service.open(self.agent, self._workflows)))
        return self

    def frontier(self) -> list[IntentWorkflow]:
        """The workflows the tool calls have produced so far."""

        embedded = self._open_gateway()
        return _wait(embedded, embedded.service.frontier(self.run_id)).context.get_workflows()

    def close(self, force: bool = False) -> list[IntentWorkflow]:
        """Close the session and return its final workflows. Idempotent."""

        if not self.closed:
            embedded = self._open_gateway()
            self._closed(_wait(embedded, embedded.service.close(self.run_id, force=force)))
        return list(self._workflows)

    def __enter__(self) -> GatewaySession:
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

    async def aopen(self) -> GatewaySession:
        embedded = self._begin()
        run = await _await(embedded, embedded.service.open(self.agent, self._workflows))
        self._opened(embedded, run)
        return self

    async def afrontier(self) -> list[IntentWorkflow]:
        embedded = self._open_gateway()
        run = await _await(embedded, embedded.service.frontier(self.run_id))
        return run.context.get_workflows()

    async def aclose(self, force: bool = False) -> list[IntentWorkflow]:
        if not self.closed:
            embedded = self._open_gateway()
            self._closed(await _await(embedded, embedded.service.close(self.run_id, force=force)))
        return list(self._workflows)

    async def __aenter__(self) -> GatewaySession:
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

    def _begin(self) -> EmbeddedGateway:
        if self.run_id:
            raise RuntimeError("the session is already open")
        return self.gateway._gateway()

    def _open_gateway(self) -> EmbeddedGateway:
        if self._embedded is None or not self.run_id:
            raise RuntimeError("the session has not been opened")
        return self._embedded

    def _opened(self, embedded: EmbeddedGateway, run: Run) -> None:
        self._embedded = embedded
        self.run_id = run.run_id
        self.servers = embedded.service.servers
        self._mcp_base = f"{embedded.url}{mcp_path(run.run_id)}"

    def _closed(self, workflows: list[IntentWorkflow]) -> None:
        self._workflows = workflows
        self.closed = True


def _wait(embedded: EmbeddedGateway, coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a gateway operation on the gateway's loop and wait for it."""

    try:
        return embedded.submit(coroutine).result()
    except RunError as exc:
        raise _error(exc) from exc


async def _await(embedded: EmbeddedGateway, coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        return await asyncio.wrap_future(embedded.submit(coroutine))
    except RunError as exc:
        raise _error(exc) from exc


def _error(exc: RunError) -> GatewayError:
    body = {"error": str(exc), **exc.details}
    return (
        WorkflowRejected(exc.status, body) if exc.status == 422 else GatewayError(exc.status, body)
    )
