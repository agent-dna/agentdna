"""Configuration for GithubAgent."""

from dotenv import load_dotenv

load_dotenv()

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ────────────────────────────────────────────────────────────────
    llm_provider: str = Field(default="gemini")  # "gemini" | "ollama" | "openai"
    gemini_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-2.0-flash")
    gemini_temperature: float = Field(default=0.1)
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="")
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="")
    openai_base_url: str = Field(default="")

    # ── GitHub ─────────────────────────────────────────────────────────────
    github_token: str = Field(default="")
    github_api_url: str = Field(default="https://api.github.com")

    # ── MCP server ─────────────────────────────────────────────────────────
    mcp_server_host: str = Field(default="127.0.0.1")
    mcp_server_port: int = Field(default=8765)

    # ── AgentDNA ───────────────────────────────────────────────────────────
    agentdna_api_key: Optional[str] = Field(default=None)

    @property
    def mcp_server_url(self) -> str:
        return f"http://{self.mcp_server_host}:{self.mcp_server_port}/mcp/"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
