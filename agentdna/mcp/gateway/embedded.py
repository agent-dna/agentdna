"""The gateway, running inside the agent's own process.

Same application as the standalone gateway, served by uvicorn on a thread of
its own with its own event loop, on a loopback port picked by the OS. Nothing
for the developer to start, configure or keep in sync: it signs with the very
`AgentDNA` instance the agent uses.

Its own thread matters. The agent's MCP client talks to it over HTTP, and the
agent may call it synchronously from inside its own event loop; a gateway on
that same loop would deadlock.
"""

from __future__ import annotations

import atexit
import secrets
import socket
import threading
import time
from dataclasses import replace

from agentdna import AgentDNA

from .config import GatewayConfig

STARTUP_TIMEOUT_SECONDS = 10.0


class EmbeddedGateway:
    def __init__(self, dna: AgentDNA, config: GatewayConfig) -> None:
        try:
            import uvicorn

            from .app import create_app
        except ImportError as exc:
            raise ImportError(
                f"the AgentDNA MCP gateway needs {exc.name}; "
                "install it with `pip install 'agent-dna[gateway]'`"
            ) from exc

        # Anything else on this machine could otherwise open runs and have
        # tool calls signed as this agent. Only this process knows the token.
        config = replace(config, token=config.token or secrets.token_urlsafe(32))
        self.token = config.token

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.host, config.port))
        host, port = sock.getsockname()[:2]
        self.url = f"http://{host}:{port}"

        self._server = uvicorn.Server(
            uvicorn.Config(
                create_app(dna, config),
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

        atexit.register(self.stop)

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
