from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from config import settings


def build_llm() -> BaseChatModel:
    if settings.llm_backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=settings.ollama_model, base_url=settings.ollama_host, temperature=settings.llm_temperature)
    if settings.llm_backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs = {"model": settings.gemini_model, "temperature": settings.llm_temperature}
        if settings.google_api_key:
            kwargs["google_api_key"] = settings.google_api_key
        return ChatGoogleGenerativeAI(**kwargs)
    raise ValueError(f"Unsupported LLM_BACKEND: {settings.llm_backend}")