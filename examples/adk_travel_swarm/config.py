from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Puts GOOGLE_API_KEY in the environment, where Google ADK reads it.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Gemini, through Google ADK
    gemini_model: str = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

    # AgentDNA
    agentdna_api_key: str = os.getenv("AGENTDNA_API_KEY", "")
    provenance_layer_url: str = (
        os.getenv("PROVENANCE_LAYER_URL") or "https://chain-connector-2.rubix.net"
    )
    admin_server_url: str = os.getenv("ADMIN_SERVER_URL") or "https://agentdna-admin.agentdna.io"
    user_name: str = os.getenv("USER_NAME", "")
    coordinator_agent_name: str = os.getenv("COORDINATOR_AGENT_NAME") or "travel-coordinator"
    weather_agent_name: str = os.getenv("WEATHER_AGENT_NAME") or "travel-weather-agent"
    destination_agent_name: str = os.getenv("DESTINATION_AGENT_NAME") or "travel-destination-agent"
    money_agent_name: str = os.getenv("MONEY_AGENT_NAME") or "travel-money-agent"

    # The travel MCP server
    mcp_server_name: str = os.getenv("MCP_SERVER_NAME") or "travel-mcp"
    travel_mcp_url: str = os.getenv("TRAVEL_MCP_URL") or "http://127.0.0.1:8660/mcp"
    mcp_host: str = os.getenv("MCP_HOST") or "127.0.0.1"
    mcp_port: int = int(os.getenv("MCP_PORT") or "8660")
    http_timeout_seconds: float = float(os.getenv("HTTP_TIMEOUT_SECONDS") or "20")


settings = Settings()
