"""Relay configuration, validated when the relay is created."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Server names appear in session URLs: /runs/{run_id}/mcp/{name}
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ConfigError(ValueError):
    """Raised when the Relay configuration is invalid."""


@dataclass(frozen=True)
class ProvenanceRelayConfig:
    # MCP server name -> its Streamable HTTP URL.
    upstreams: Mapping[str, str] = field(default_factory=dict)
    host: str = "127.0.0.1"
    # 0: the OS picks a free port, so any number of relays can coexist.
    port: int = 0
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

        # A private copy: the config must not change under a running relay.
        object.__setattr__(self, "upstreams", dict(self.upstreams))
        if not 0 <= self.port < 65536:
            raise ConfigError(f"port must be a valid TCP port, got {self.port}")
        if self.run_ttl_seconds <= 0:
            raise ConfigError("run_ttl_seconds must be positive")
        if self.upstream_timeout_seconds <= 0:
            raise ConfigError("upstream_timeout_seconds must be positive")
