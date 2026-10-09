"""Run the gateway: `agentdna-gateway` or `python -m agentdna.mcp.gateway`.

Configured entirely from the environment; see `agentdna.mcp.gateway.config`.
A `.env` file in the working directory (or a parent) is loaded first, so the
gateway can share the agent application's `.env`. Variables already set in
the environment take precedence.
"""

from __future__ import annotations

import sys

from dotenv import find_dotenv, load_dotenv

from agentdna import AgentDNA

from .config import AgentIdentityConfig, ConfigError, GatewayConfig


def build_dna(identity: AgentIdentityConfig) -> AgentDNA:
    """The gateway's AgentDNA instance, initialised the way an agent's is."""

    kwargs: dict = {
        "name": identity.name,
        "type": "agent",
        "api_key": identity.api_key,
        "config_dir": identity.config_dir,
        "agent_policy_file": identity.policy_file,
        # Registering an agent uploads its policy. Without one, leave
        # registration to the agent application that owns the policy.
        "skip_actor_id_registration": identity.policy_file is None,
    }
    if identity.provenance_layer_url:
        kwargs["provenance_layer_url"] = identity.provenance_layer_url
    if identity.admin_server_url:
        kwargs["admin_server_url"] = identity.admin_server_url
    return AgentDNA(**kwargs)


def main() -> None:
    try:
        import uvicorn

        from .app import create_app
    except ImportError as exc:
        sys.exit(
            f"agentdna-gateway: missing dependency ({exc.name}). "
            "Install it with `pip install 'agent-dna[gateway]'`."
        )

    load_dotenv(find_dotenv(usecwd=True))

    try:
        identity = AgentIdentityConfig.from_env()
        config = GatewayConfig.from_env()
    except ConfigError as exc:
        sys.exit(f"agentdna-gateway: {exc}")

    try:
        dna = build_dna(identity)
    except Exception as exc:
        sys.exit(f"agentdna-gateway: unable to initialise AgentDNA: {exc}")

    uvicorn.run(create_app(dna, config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
