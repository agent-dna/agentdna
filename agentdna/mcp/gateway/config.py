"""Gateway configuration.

In-process gateways (`AgentDNA.mcp_gateway(servers)`) are configured in code.
A standalone gateway (`agentdna-gateway`) reads the environment. Either way
everything is validated at startup so a misconfigured gateway refuses to start
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

    AGENTDNA_GATEWAY_UPSTREAMS         Required. The MCP servers to front, by name:
                                       "github=http://127.0.0.1:8011/mcp,jira=https://..."
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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ENV_AGENT_NAME = "AGENTDNA_AGENT_NAME"
ENV_API_KEY = "AGENTDNA_API_KEY"
ENV_PROVENANCE_LAYER_URL = "AGENTDNA_PROVENANCE_LAYER_URL"
ENV_ADMIN_SERVER_URL = "AGENTDNA_ADMIN_SERVER_URL"
ENV_CONFIG_DIR = "AGENTDNA_CONFIG_DIR"
ENV_AGENT_POLICY_FILE = "AGENTDNA_AGENT_POLICY_FILE"

ENV_UPSTREAMS = "AGENTDNA_GATEWAY_UPSTREAMS"
ENV_HOST = "AGENTDNA_GATEWAY_HOST"
ENV_PORT = "AGENTDNA_GATEWAY_PORT"
ENV_TOKEN = "AGENTDNA_GATEWAY_TOKEN"
ENV_RUN_TTL_SECONDS = "AGENTDNA_GATEWAY_RUN_TTL_SECONDS"
ENV_UPSTREAM_TIMEOUT_SECONDS = "AGENTDNA_GATEWAY_UPSTREAM_TIMEOUT_SECONDS"


# Server names appear in run URLs: /runs/{run_id}/mcp/{name}
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


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
    # MCP server name -> its Streamable HTTP URL.
    upstreams: Mapping[str, str] = field(default_factory=dict)
    host: str = "127.0.0.1"
    port: int = 8090
    token: str = ""
    run_ttl_seconds: float = 3600.0
    upstream_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.upstreams:
            raise ConfigError("at least one MCP server is required")
        for name, url in self.upstreams.items():
            if not _SERVER_NAME.match(name):
                raise ConfigError(
                    f"MCP server name {name!r} may only contain letters, digits, '-' and '_'"
                )
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigError(f"MCP server {name!r} must have an http(s) URL, got {url!r}")
        # A private copy: the config must not change under a running gateway.
        object.__setattr__(self, "upstreams", dict(self.upstreams))
        if not 0 <= self.port < 65536:
            raise ConfigError(f"{ENV_PORT} must be a valid TCP port, got {self.port}")
        if self.run_ttl_seconds <= 0:
            raise ConfigError(f"{ENV_RUN_TTL_SECONDS} must be positive")
        if self.upstream_timeout_seconds <= 0:
            raise ConfigError(f"{ENV_UPSTREAM_TIMEOUT_SECONDS} must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        env = os.environ if env is None else env

        return cls(
            upstreams=parse_upstreams(env.get(ENV_UPSTREAMS, "")),
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


def parse_upstreams(raw: str) -> dict[str, str]:
    """`name=url,name=url` -> {name: url}."""

    if not raw.strip():
        raise ConfigError(f"{ENV_UPSTREAMS} is required, e.g. 'github=http://127.0.0.1:8011/mcp'")

    upstreams: dict[str, str] = {}
    for entry in raw.split(","):
        name, sep, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ConfigError(f"{ENV_UPSTREAMS} entries must look like name=url, got {entry!r}")
        if name in upstreams:
            raise ConfigError(f"{ENV_UPSTREAMS} names MCP server {name!r} twice")
        upstreams[name] = url
    return upstreams
