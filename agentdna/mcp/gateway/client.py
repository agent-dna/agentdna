"""What an agent uses to have its MCP tool calls signed.

    AGENT = AgentDNA(name="github-agent", type="agent", ...)
    MCP = AGENT.mcp_gateway({"github": "http://127.0.0.1:8011/mcp"})

    async with MCP.session(incoming_workflow) as session:
        client = MultiServerMCPClient(
            {"github": {"transport": "streamable_http", "url": session.url("github")}}
        )
        output = await agent.ainvoke(...)

    reply = session.reply(str(output))

`session()` verifies the incoming workflow. Every tool call made through
`session.url(...)` - by any MCP client, unpatched - is signed as AGENT and
builds on it. `reply()` signs the agent's answer on top of whatever those tool
calls produced. `with` works the same way in synchronous code.

With `servers`, the gateway runs inside this process and starts on first use.
With `url`, it is a standalone gateway (`agentdna-gateway`) running elsewhere.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

from agentdna.error import RESULT_OK
from agentdna.types import IntentWorkflow, load_workflow

from .config import GatewayConfig

if TYPE_CHECKING:
    from agentdna import AgentDNA

    from .embedded import EmbeddedGateway


class GatewayError(RuntimeError):
    """The gateway refused or failed a request."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = body.get("error") if isinstance(body, dict) else body
        super().__init__(f"AgentDNA MCP gateway returned {status}: {message}")


class WorkflowRejected(GatewayError):
    """The incoming workflow failed AgentDNA verification."""

    @property
    def status_code(self) -> int | None:
        return self.body.get("status_code") if isinstance(self.body, dict) else None


class MCPGateway:
    """Signs an agent's MCP tool calls. Usually made with `AgentDNA.mcp_gateway()`."""

    def __init__(
        self,
        dna: AgentDNA,
        servers: Mapping[str, str] | None = None,
        *,
        url: str = "",
        token: str = "",
        timeout: float = 30.0,
    ) -> None:
        if bool(servers) == bool(url):
            raise ValueError("pass either `servers` (in-process gateway) or `url`, not both")

        self.dna = dna
        self.timeout = timeout
        self._config = GatewayConfig(upstreams=dict(servers)) if servers else None
        self._url = url.rstrip("/")
        self._token = token
        self._embedded: EmbeddedGateway | None = None
        self._lock = threading.Lock()

    def session(self, workflows: IntentWorkflow | list[IntentWorkflow]) -> GatewaySession:
        """A session on the workflow(s) the agent received. Opens on entering `with`."""

        if isinstance(workflows, IntentWorkflow):
            workflows = [workflows]
        if not workflows:
            raise ValueError("a session needs at least one workflow to build on")
        return GatewaySession(self, list(workflows))

    def close(self) -> None:
        """Stop the in-process gateway, if one was started. Also runs at exit."""

        with self._lock:
            if self._embedded is not None:
                self._embedded.stop()
                self._embedded = None

    def _endpoint(self) -> tuple[str, str]:
        if self._config is None:
            return self._url, self._token
        with self._lock:
            if self._embedded is None:
                from .embedded import EmbeddedGateway

                self._embedded = EmbeddedGateway(self.dna, self._config)
            return self._embedded.url, self._embedded.token


class GatewaySession:
    """One agent execution on the gateway. Usable with `with` or `async with`."""

    def __init__(self, gateway: MCPGateway, workflows: list[IntentWorkflow]) -> None:
        self.gateway = gateway
        self.workflows: list[IntentWorkflow] = workflows
        self.run_id = ""
        self.servers: list[str] = []
        self.closed = False
        self._base = ""
        self._mcp_base = ""
        self._headers: dict[str, str] = {}

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

    def reply(
        self,
        payload: str,
        recipient_id: str = "",
        verification_code: int = RESULT_OK,
    ) -> IntentWorkflow:
        """Sign the agent's answer on top of everything its tool calls produced."""

        self.close()
        return self.gateway.dna.build(
            payload=payload,
            previous_workflows=self.workflows,
            recipient_id=recipient_id,
            verification_code=verification_code,
        )

    async def areply(
        self,
        payload: str,
        recipient_id: str = "",
        verification_code: int = RESULT_OK,
    ) -> IntentWorkflow:
        await self.aclose()
        return self.gateway.dna.build(
            payload=payload,
            previous_workflows=self.workflows,
            recipient_id=recipient_id,
            verification_code=verification_code,
        )

    # -- sync ---------------------------------------------------------------

    def open(self) -> GatewaySession:
        body = self._open_body()
        with httpx.Client(timeout=self.gateway.timeout) as client:
            response = client.post(f"{self._base}/runs", json=body, headers=self._headers)
        self._opened(response)
        return self

    def frontier(self) -> list[IntentWorkflow]:
        """The workflows the tool calls have produced so far."""

        with httpx.Client(timeout=self.gateway.timeout) as client:
            response = client.get(self._run_url, headers=self._headers)
        return _workflows(_body(response))

    def close(self, force: bool = False) -> list[IntentWorkflow]:
        """Close the session and return its final workflows. Idempotent."""

        if not self.closed:
            with httpx.Client(timeout=self.gateway.timeout) as client:
                response = client.delete(self._run_url, params=_force(force), headers=self._headers)
            self._closed(response)
        return self.workflows

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
        body = self._open_body()
        async with httpx.AsyncClient(timeout=self.gateway.timeout) as client:
            response = await client.post(f"{self._base}/runs", json=body, headers=self._headers)
        self._opened(response)
        return self

    async def afrontier(self) -> list[IntentWorkflow]:
        async with httpx.AsyncClient(timeout=self.gateway.timeout) as client:
            response = await client.get(self._run_url, headers=self._headers)
        return _workflows(_body(response))

    async def aclose(self, force: bool = False) -> list[IntentWorkflow]:
        if not self.closed:
            async with httpx.AsyncClient(timeout=self.gateway.timeout) as client:
                response = await client.delete(
                    self._run_url, params=_force(force), headers=self._headers
                )
            self._closed(response)
        return self.workflows

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

    @property
    def _run_url(self) -> str:
        if not self.run_id:
            raise RuntimeError("the session has not been opened")
        return f"{self._base}/runs/{self.run_id}"

    def _open_body(self) -> dict[str, Any]:
        if self.run_id:
            raise RuntimeError("the session is already open")
        # Starts an in-process gateway on first use. Blocking, but only once.
        self._base, token = self.gateway._endpoint()
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        return {"workflows": [json.loads(w.serialize()) for w in self.workflows]}

    def _opened(self, response: httpx.Response) -> None:
        if response.status_code == 422:
            raise WorkflowRejected(response.status_code, _json(response))
        body = _body(response, expected=201)
        self.run_id = body["run_id"]
        self.servers = list(body.get("servers", []))
        self._mcp_base = f"{self._base}{body['mcp_path']}"

    def _closed(self, response: httpx.Response) -> None:
        self.workflows = _workflows(_body(response))
        self.closed = True


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _body(response: httpx.Response, expected: int = 200) -> dict[str, Any]:
    body = _json(response)
    if response.status_code != expected or not isinstance(body, dict):
        raise GatewayError(response.status_code, body)
    return body


def _workflows(body: dict[str, Any]) -> list[IntentWorkflow]:
    return [load_workflow(item) for item in body["workflows"]]


def _force(force: bool) -> dict[str, str]:
    return {"force": "true"} if force else {}
