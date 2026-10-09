"""What the agent application uses to hand its workflow to the gateway.

The gateway runs in another process, so the workflow the agent received cannot
reach it through an ambient context. Instead the agent opens a run with it,
points its MCP client at the run's URL - any MCP client, unpatched - and
closes the run to get back the frontier to build its own reply on:

    gateway = GatewayClient("http://127.0.0.1:8090")

    with gateway.run(incoming_workflow) as run:
        tools = connect_mcp_client(url=run.mcp_url)
        output = agent.invoke(task, tools)
        workflows = run.close()

    reply = AGENT.build(payload=output, previous_workflows=workflows)

`async with` works the same way, with `await run.aclose()`.

Do not also install a client adapter (`install_mcp_client`) on that MCP
client: the gateway is doing that job.
"""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any

import httpx

from agentdna.types import IntentWorkflow, load_workflow


class GatewayError(RuntimeError):
    """The gateway refused or failed a run request."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = body.get("error") if isinstance(body, dict) else body
        super().__init__(f"AgentDNA gateway returned {status}: {message}")


class GatewayClient:
    def __init__(self, url: str, token: str = "", timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def run(self, workflows: IntentWorkflow | list[IntentWorkflow]) -> GatewayRun:
        """A run on these workflows. It is opened on entering the `with` block."""

        if isinstance(workflows, IntentWorkflow):
            workflows = [workflows]
        if not workflows:
            raise ValueError("a run needs at least one workflow to build on")
        return GatewayRun(self, list(workflows))

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


class GatewayRun:
    """One run on the gateway. Usable with `with` or `async with`."""

    def __init__(self, gateway: GatewayClient, workflows: list[IntentWorkflow]) -> None:
        self.gateway = gateway
        self.workflows: list[IntentWorkflow] = workflows
        self.run_id = ""
        self.mcp_url = ""
        self.closed = False

    # -- sync ---------------------------------------------------------------

    def open(self) -> GatewayRun:
        with httpx.Client(timeout=self.gateway.timeout) as client:
            response = client.post(
                f"{self.gateway.url}/runs",
                json=self._open_body(),
                headers=self.gateway._headers,
            )
        self._opened(_body(response, expected=201))
        return self

    def frontier(self) -> list[IntentWorkflow]:
        """The run's current frontier, without closing it."""

        with httpx.Client(timeout=self.gateway.timeout) as client:
            response = client.get(self._run_url, headers=self.gateway._headers)
        return _workflows(_body(response))

    def close(self, force: bool = False) -> list[IntentWorkflow]:
        """Close the run and return its final frontier. Idempotent."""

        if not self.closed:
            with httpx.Client(timeout=self.gateway.timeout) as client:
                response = client.delete(
                    self._run_url, params=_force(force), headers=self.gateway._headers
                )
            self._closed(_body(response))
        return self.workflows

    def __enter__(self) -> GatewayRun:
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

    async def aopen(self) -> GatewayRun:
        async with httpx.AsyncClient(timeout=self.gateway.timeout) as client:
            response = await client.post(
                f"{self.gateway.url}/runs",
                json=self._open_body(),
                headers=self.gateway._headers,
            )
        self._opened(_body(response, expected=201))
        return self

    async def afrontier(self) -> list[IntentWorkflow]:
        async with httpx.AsyncClient(timeout=self.gateway.timeout) as client:
            response = await client.get(self._run_url, headers=self.gateway._headers)
        return _workflows(_body(response))

    async def aclose(self, force: bool = False) -> list[IntentWorkflow]:
        if not self.closed:
            async with httpx.AsyncClient(timeout=self.gateway.timeout) as client:
                response = await client.delete(
                    self._run_url, params=_force(force), headers=self.gateway._headers
                )
            self._closed(_body(response))
        return self.workflows

    async def __aenter__(self) -> GatewayRun:
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
            raise RuntimeError("the run has not been opened")
        return f"{self.gateway.url}/runs/{self.run_id}"

    def _open_body(self) -> dict[str, Any]:
        if self.run_id:
            raise RuntimeError("the run is already open")
        return {"workflows": [json.loads(w.serialize()) for w in self.workflows]}

    def _opened(self, body: dict[str, Any]) -> None:
        self.run_id = body["run_id"]
        self.mcp_url = f"{self.gateway.url}{body['mcp_path']}"

    def _closed(self, body: dict[str, Any]) -> None:
        self.workflows = _workflows(body)
        self.closed = True


def _body(response: httpx.Response, expected: int = 200) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if response.status_code != expected or not isinstance(body, dict):
        raise GatewayError(response.status_code, body)
    return body


def _workflows(body: dict[str, Any]) -> list[IntentWorkflow]:
    return [load_workflow(item) for item in body["workflows"]]


def _force(force: bool) -> dict[str, str]:
    return {"force": "true"} if force else {}
