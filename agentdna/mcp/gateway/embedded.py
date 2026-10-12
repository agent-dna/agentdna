"""The gateway, running inside the agent's own process.

The gateway application, served by uvicorn on a thread of its own with its own
event loop, on a loopback port picked by the OS. Nothing for the developer to
start or configure, and no identity of its own: each session signs with the
`AgentDNA` instance of the agent that opened it.

Runs are opened and closed by calling the gateway directly, not over HTTP, so
nothing else on the machine can open a run and have tool calls signed as an
agent. The only HTTP surface is each run's MCP endpoint, reachable only
through its unguessable URL.

Its own thread matters. The agent's MCP client talks to it over HTTP, and the
agent may wait on it synchronously from inside its own event loop; a gateway
on that same loop would deadlock.
"""

from __future__ import annotations

import asyncio
import atexit
import socket
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from .config import GatewayConfig
from .service import RunService

STARTUP_TIMEOUT_SECONDS = 10.0

T = TypeVar("T")


class EmbeddedGateway:
    def __init__(self, config: GatewayConfig) -> None:
        import uvicorn

        from .app import create_app

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.host, config.port))
        host, port = sock.getsockname()[:2]
        self.url = f"http://{host}:{port}"

        app = create_app(config)
        self.service: RunService = app.state.service

        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_config=None,  # leave the host application's logging alone
                access_log=False,
                lifespan="on",
            )
        )
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [sock]},
            name="agentdna-mcp-gateway",
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("the AgentDNA MCP gateway failed to start")
            time.sleep(0.01)

        # Set by the app's lifespan, which has run by the time `started` is set.
        self._loop: asyncio.AbstractEventLoop = app.state.loop

        atexit.register(self.stop)

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        """Run a gateway operation on the gateway's own event loop."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
