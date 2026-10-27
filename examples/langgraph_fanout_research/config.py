from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Puts GOOGLE_API_KEY in the environment, where langchain-google-genai reads it.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Gemini
    gemini_model: str = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE") or "0")

    # Fan-out: the most workers the orchestrator may spawn for one question.
    max_workers: int = int(os.getenv("MAX_WORKERS") or "4")

    # The research MCP server
    research_mcp_url: str = os.getenv("RESEARCH_MCP_URL") or "http://127.0.0.1:8670/mcp"
    mcp_host: str = os.getenv("MCP_HOST") or "127.0.0.1"
    mcp_port: int = int(os.getenv("MCP_PORT") or "8670")
    http_timeout_seconds: float = float(os.getenv("HTTP_TIMEOUT_SECONDS") or "45")

    # Wikipedia: a URL or email to reach you by, sent in the User-Agent as
    # Wikimedia's robot policy requires, and the most concurrent requests.
    contact: str = os.getenv("CONTACT", "")
    wikipedia_concurrency: int = int(os.getenv("WIKIPEDIA_CONCURRENCY") or "2")

    # AgentDNA
    agentdna_api_key: str = os.getenv("AGENTDNA_API_KEY", "")
    provenance_layer_url: str = (
        os.getenv("PROVENANCE_LAYER_URL") or "https://chain-connector-2.rubix.net"
    )
    admin_server_url: str = os.getenv("ADMIN_SERVER_URL") or "https://agentdna-admin.agentdna.io"
    user_name: str = os.getenv("USER_NAME", "")
    orchestrator_agent_name: str = os.getenv("ORCHESTRATOR_AGENT_NAME") or "research-orchestrator"
    mcp_server_name: str = os.getenv("MCP_SERVER_NAME") or "research-mcp"
    # CBAC decision service. Empty: the `cbac` package's own default (production).
    cbac_url: str = os.getenv("CBAC_URL", "")


settings = Settings()
