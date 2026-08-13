"""
LLM factory — one switch for the whole project.

Set ``LLM_PROVIDER`` to choose the backend:

  - ``gemini`` (default)
      Uses the Gemini API
      (``GEMINI_MODEL`` / ``GEMINI_API_KEY``)

  - ``ollama``
      Uses a local Ollama server
      (``OLLAMA_BASE_URL`` / ``OLLAMA_MODEL``)

  - ``openai``
      Uses OpenAI or any OpenAI-compatible API
      (``OPENAI_MODEL`` / ``OPENAI_API_KEY``)

      If ``OPENAI_BASE_URL`` is provided, requests are sent to that endpoint.
      Otherwise, the official OpenAI API is used.

Both agents (and any future ones) build their model through ``make_llm`` so the
provider can be switched entirely through configuration.

Note:
The Worker drives tools via ``create_react_agent``, which requires a model that
supports tool/function calling. The Coordinator only generates text, so any
chat model is sufficient.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from app.config import settings


def make_llm(temperature: float = 0.0) -> BaseChatModel:
    """Construct the configured chat model."""

    provider = (settings.llm_provider or "gemini").strip().lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.gemini_model:
            raise ValueError("LLM_PROVIDER=gemini but GEMINI_MODEL is not set.")

        if not settings.gemini_api_key:
            raise ValueError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set.")

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.gemini_api_key,
            temperature=temperature,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        if not settings.ollama_model:
            raise ValueError("LLM_PROVIDER=ollama but OLLAMA_MODEL is not set.")

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if not settings.openai_model:
            raise ValueError("LLM_PROVIDER=openai but OPENAI_MODEL is not set.")

        if not settings.openai_api_key:
            raise ValueError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set.")

        kwargs = {
            "model": settings.openai_model,
            "api_key": settings.openai_api_key,
            "temperature": temperature,
        }

        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url

        return ChatOpenAI(**kwargs)

    raise ValueError(f"Unknown LLM_PROVIDER={provider!r}; expected 'gemini', 'ollama' or 'openai'.")
