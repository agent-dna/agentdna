from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    # LLM
    llm_backend: str
    llm_temperature: float

    # Ollama
    ollama_host: str
    ollama_model: str

    # OpenAI
    openai_model: str
    openai_base_url: str | None

    # Gemini
    gemini_model: str

    # MCP
    trello_mcp_url: str | None

    # AgentDNA related attributes
    agentdna_api_key: str
    agentdna_provenance_url: str

    user_name: str
    agent_name: str



settings = Settings(
    llm_backend=os.getenv("LLM_BACKEND", "ollama").lower(),
    llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
    ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
    ollama_model=os.getenv("OLLAMA_MODEL", "llama3.1"),
    openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    openai_base_url=os.getenv("OPENAI_BASE_URL"),
    gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
    trello_mcp_url=os.getenv("TRELLO_MCP_URL"),
    agentdna_api_key=os.getenv("AGENTDNA_API_KEY", ""),
    agentdna_provenance_url=os.getenv("AGENTDNA_PROVENANCE_URL", "https://chain-connector-2.rubix.net"),
    user_name=os.getenv("USER_NAME", ""),
    agent_name=os.getenv("AGENT_NAME", ""),
)
