"""
LLM factory — one switch for the whole project.

Set ``LLM_PROVIDER`` to choose the backend:
  - ``gemini`` (default) → the Gemini API (``GEMINI_MODEL`` / ``GEMINI_API_KEY``)
  - ``ollama``           → a local Ollama server (``OLLAMA_BASE_URL`` / ``OLLAMA_MODEL``)

Both agents (and any future ones) build their model through ``make_llm`` so the
provider can be flipped from config without touching agent code.

Note: the Worker drives tools via ``create_react_agent``, which needs a model
that supports tool/function calling. The Coordinator only generates text, so any
model works there. With Ollama, point the Worker at a tool-capable model
(e.g. ``llama3.1``, ``qwen2.5``, ``mistral-nemo``) — a plain text-generation GGUF
may not emit tool calls.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.config import settings


def make_llm(temperature: float = 0.0) -> BaseChatModel:
    """Construct the chat model for the configured provider."""
    provider = (settings.llm_provider or "gemini").strip().lower()

    if provider == "ollama":
        # Lazy import so langchain-ollama is only required when actually used.
        from langchain_ollama import ChatOllama

        if not settings.ollama_model:
            raise ValueError(
                "LLM_PROVIDER=ollama but OLLAMA_MODEL is not set — set it to the "
                "model name served by your Ollama server."
            )
        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
        )

    if provider != "gemini":
        raise ValueError(
            f"Unknown LLM_PROVIDER={provider!r}; expected 'gemini' or 'ollama'."
        )

    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        temperature=temperature,
    )
