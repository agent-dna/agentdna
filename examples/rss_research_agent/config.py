from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_FEEDS = [
    {"id": "cisa", "name": "CISA Alerts", "url": "https://www.cisa.gov/cybersecurity-advisories/all.xml"},
    {"id": "github", "name": "GitHub Blog", "url": "https://github.blog/feed/"},
]


@dataclass(frozen=True)
class Settings:
    llm_backend: str = os.getenv("LLM_BACKEND", "ollama").lower()
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    llm_request_timeout_seconds: float = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120"))
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "4"))
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    rss_mcp_url: str = os.getenv("RSS_MCP_URL", "http://127.0.0.1:8013/mcp")
    mcp_host: str = os.getenv("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(os.getenv("MCP_PORT", "8013"))
    mcp_timeout_seconds: float = float(os.getenv("MCP_TOOL_TIMEOUT_SECONDS", "30"))
    cache_database: Path = Path(os.getenv("RSS_CACHE_DATABASE", "data/rss_cache.db"))
    feeds_json: str = os.getenv("RSS_FEEDS_JSON", json.dumps(DEFAULT_FEEDS))
    research_task: str = os.getenv("RSS_RESEARCH_TASK", "Research material developments relevant to security and technology teams.")

    agentdna_api_key: str = os.getenv("AGENTDNA_API_KEY", "")
    provenance_layer_url: str = os.getenv("PROVENANCE_LAYER_URL", "https://chain-connector-2.rubix.net")

    user_name: str = os.getenv("USER_NAME", "user_123")
    orchestrator_agent_name: str = os.getenv("RSS_ORCHESTRATOR_AGENT_NAME", "RSS Orchestrator Agent")
    technology_agent_name: str = os.getenv("RSS_TECHNOLOGY_AGENT_NAME", "RSS Technology Agent")
    security_agent_name: str = os.getenv("RSS_SECURITY_AGENT_NAME", "RSS Security Agent")

    @property
    def feeds(self) -> list[dict[str, str]]:
        feeds = json.loads(self.feeds_json)
        if not isinstance(feeds, list) or not feeds:
            raise ValueError("RSS_FEEDS_JSON must be a non-empty JSON array")
        for feed in feeds:
            if not {"id", "name", "url"}.issubset(feed) or not str(feed["url"]).startswith("https://"):
                raise ValueError("Each RSS feed must include id, name, and an HTTPS url")
        return feeds


settings = Settings()