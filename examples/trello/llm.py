from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from config import settings


def make_llm():
    if settings.llm_backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_host,
            temperature=settings.llm_temperature,
        )

    if settings.llm_backend == "openai":
        from langchain_openai import ChatOpenAI

        kwargs = {"models": settings.openai_model, "temperature": settings.llm_temperature}

        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url

        return ChatOpenAI(**kwargs)

    if settings.llm_backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=settings.llm_temperature,
        )

    raise ValueError(f"Unsupported LLM backend: {settings.llm_backend}")
