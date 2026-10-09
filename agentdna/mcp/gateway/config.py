"""Gateway configuration, read from the environment.

Everything is validated at startup so a misconfigured gateway refuses to start
instead of failing on the first tool call.

The agent identity:

    AGENTDNA_AGENT_NAME            Required. The AgentDNA name the gateway signs as.
    AGENTDNA_API_KEY               AgentDNA API key.
    AGENTDNA_PROVENANCE_LAYER_URL  Provenance layer URL. SDK default when unset.
    AGENTDNA_ADMIN_SERVER_URL      Admin server URL. SDK default when unset.
    AGENTDNA_CONFIG_DIR            Actor config directory. SDK default when unset.
    AGENTDNA_AGENT_POLICY_FILE     Agent policy. When set, the agent is registered
                                   at startup; when unset, registration is skipped
                                   and left to the agent application.

The gateway itself:

    AGENTDNA_GATEWAY_UPSTREAM_URL      Required. Streamable HTTP URL of the MCP server.
    AGENTDNA_GATEWAY_HOST              Bind address. Default 127.0.0.1.
    AGENTDNA_GATEWAY_PORT              Bind port. Default 8090.
    AGENTDNA_GATEWAY_TOKEN             When set, required as a bearer token on the
                                       run endpoints.
    AGENTDNA_GATEWAY_RUN_TTL_SECONDS   Idle runs are dropped after this. Default 3600.
    AGENTDNA_GATEWAY_UPSTREAM_TIMEOUT_SECONDS
                                       Read timeout for one upstream request. Default 300.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ENV_AGENT_NAME = "AGENTDNA_AGENT_NAME"
ENV_API_KEY = "AGENTDNA_API_KEY"
ENV_PROVENANCE_LAYER_URL = "AGENTDNA_PROVENANCE_LAYER_URL"
ENV_ADMIN_SERVER_URL = "AGENTDNA_ADMIN_SERVER_URL"
ENV_CONFIG_DIR = "AGENTDNA_CONFIG_DIR"
ENV_AGENT_POLICY_FILE = "AGENTDNA_AGENT_POLICY_FILE"

ENV_UPSTREAM_URL = "AGENTDNA_GATEWAY_UPSTREAM_URL"
ENV_HOST = "AGENTDNA_GATEWAY_HOST"
ENV_PORT = "AGENTDNA_GATEWAY_PORT"
ENV_TOKEN = "AGENTDNA_GATEWAY_TOKEN"
ENV_RUN_TTL_SECONDS = "AGENTDNA_GATEWAY_RUN_TTL_SECONDS"
ENV_UPSTREAM_TIMEOUT_SECONDS = "AGENTDNA_GATEWAY_UPSTREAM_TIMEOUT_SECONDS"


class ConfigError(ValueError):
    """Raised when the gateway configuration is missing or invalid."""


@dataclass(frozen=True)
class AgentIdentityConfig:
    """What the gateway needs to initialise its `AgentDNA` instance."""

    name: str
    api_key: str = ""
    provenance_layer_url: str = ""
    admin_server_url: str = ""
    config_dir: str = ""
    policy_file: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgentIdentityConfig:
        env = os.environ if env is None else env

        name = env.get(ENV_AGENT_NAME, "").strip()
        if not name:
            raise ConfigError(f"{ENV_AGENT_NAME} is required")

        policy = env.get(ENV_AGENT_POLICY_FILE, "").strip()
        policy_file = Path(policy) if policy else None
        if policy_file is not None and not policy_file.is_file():
            raise ConfigError(f"{ENV_AGENT_POLICY_FILE} does not point to a file: {policy}")

        return cls(
            name=name,
            api_key=env.get(ENV_API_KEY, "").strip(),
            provenance_layer_url=env.get(ENV_PROVENANCE_LAYER_URL, "").strip(),
            admin_server_url=env.get(ENV_ADMIN_SERVER_URL, "").strip(),
            config_dir=env.get(ENV_CONFIG_DIR, "").strip(),
            policy_file=policy_file,
        )


@dataclass(frozen=True)
class GatewayConfig:
    upstream_url: str
    host: str = "127.0.0.1"
    port: int = 8090
    token: str = ""
    run_ttl_seconds: float = 3600.0
    upstream_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.upstream_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"{ENV_UPSTREAM_URL} must be an http(s) URL, got {self.upstream_url!r}"
            )
        if not 0 < self.port < 65536:
            raise ConfigError(f"{ENV_PORT} must be a valid TCP port, got {self.port}")
        if self.run_ttl_seconds <= 0:
            raise ConfigError(f"{ENV_RUN_TTL_SECONDS} must be positive")
        if self.upstream_timeout_seconds <= 0:
            raise ConfigError(f"{ENV_UPSTREAM_TIMEOUT_SECONDS} must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        env = os.environ if env is None else env

        upstream_url = env.get(ENV_UPSTREAM_URL, "").strip()
        if not upstream_url:
            raise ConfigError(f"{ENV_UPSTREAM_URL} is required")

        return cls(
            upstream_url=upstream_url,
            host=env.get(ENV_HOST, "").strip() or "127.0.0.1",
            port=_number(env, ENV_PORT, 8090, int),
            token=env.get(ENV_TOKEN, "").strip(),
            run_ttl_seconds=_number(env, ENV_RUN_TTL_SECONDS, 3600.0, float),
            upstream_timeout_seconds=_number(env, ENV_UPSTREAM_TIMEOUT_SECONDS, 300.0, float),
        )


def _number(env: Mapping[str, str], name: str, default, kind):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")
