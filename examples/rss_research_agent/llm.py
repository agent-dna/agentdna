from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage

from config import settings


def message_text(message: BaseMessage) -> str:
    """A model message's text. Some providers (Gemini) return content as a list
    of parts, which `str()` would turn into a Python list literal."""

    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        part if isinstance(part, str) else str(part.get("text", ""))
        for part in content
        if isinstance(part, str) or part.get("type") == "text"
    )


def build_llm() -> BaseChatModel:
    if settings.llm_backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=settings.ollama_model, base_url=settings.ollama_host, temperature=settings.llm_temperature)
    if settings.llm_backend == "openai":
        from langchain_openai import ChatOpenAI

        kwargs = {
            "model": settings.openai_name,
            "temperature": settings.llm_temperature,
            "timeout": settings.llm_request_timeout_seconds,
            "max_retries": settings.llm_max_retries,
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        if settings.openai_api_key:
            kwargs["api_key"] = settings.openai_api_key
    
        return ChatOpenAI(**kwargs)

    if settings.llm_backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs = {
            "model": settings.gemini_model,
            "temperature": settings.llm_temperature,
            "timeout": settings.llm_request_timeout_seconds,
            "max_retries": settings.llm_max_retries,
        }
        if settings.google_api_key:
            kwargs["google_api_key"] = settings.google_api_key
        return ChatGoogleGenerativeAI(**kwargs)
    raise ValueError(f"Unsupported LLM_BACKEND: {settings.llm_backend}")