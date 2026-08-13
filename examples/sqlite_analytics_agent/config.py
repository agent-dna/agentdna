from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    agent_id: str = "sqlite-analytics-agent"
    llm_backend: str = os.getenv("LLM_BACKEND", "ollama").lower()
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_base_url: str | None = os.getenv("OPENAI_BASE_URL")
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    database_path: Path = Path(os.getenv("SQLITE_DATABASE_PATH", "data/analytics.db"))
    analysis_task: str = os.getenv(
        "SQLITE_ANALYSIS_TASK",
        "Analyse recent business activity, anomalies, strong products, and useful recommendations.",
    )

    agentdna_api_key: str = os.getenv("AGENTDNA_API_KEY", "")
    provenance_layer_url: str = os.getenv("PROVENANCE_LAYER_URL", "https://chain-connector-2.rubix.net")
    user_name: str = os.getenv("USER_NAME", "")
    sqlite_agent_name: str = os.getenv("SQLITE_AGENT_NAME", "")

settings = Settings()