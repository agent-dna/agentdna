from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    agent_id: str = "github-repository-agent"
    llm_backend: str = os.getenv("LLM_BACKEND", "ollama").lower()
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_base_url: str | None = os.getenv("OPENAI_BASE_URL")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_api_url: str = os.getenv("GITHUB_API_URL", "https://api.github.com")
    github_repository_owner: str = os.getenv("GITHUB_REPOSITORY_OWNER", "")
    github_repository_name: str = os.getenv("GITHUB_REPOSITORY_NAME", "")
    github_mcp_url: str = os.getenv("GITHUB_MCP_URL", "http://127.0.0.1:8011/mcp")
    mcp_host: str = os.getenv("MCP_HOST", "127.0.0.1")
    mcp_port: int = int(os.getenv("MCP_PORT", "8011"))
    mcp_timeout_seconds: float = float(os.getenv("MCP_TOOL_TIMEOUT_SECONDS", "30"))
    analysis_task: str = os.getenv(
        "GITHUB_ANALYSIS_TASK",
        "Analyse recent repository activity, notable issues, pull requests, changes, and risks.",
    )

    # AgentDNA related env variables
    agentdna_api_key: str = os.getenv("AGENTDNA_API_KEY", "")
    provenance_layer_url: str = os.getenv("PROVENANCE_LAYER_URL", "https://chain-connector-2.rubix.net")
    user_name: str = os.getenv("USER_NAME", "")
    github_agent_name: str = os.getenv("GITHUB_AGENT_NAME", "")

    @property
    def repository(self) -> str:
        if not self.github_repository_owner or not self.github_repository_name:
            raise ValueError("GITHUB_REPOSITORY_OWNER and GITHUB_REPOSITORY_NAME are required")
        return f"{self.github_repository_owner}/{self.github_repository_name}"


settings = Settings()