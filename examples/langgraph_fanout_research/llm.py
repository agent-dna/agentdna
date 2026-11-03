from __future__ import annotations

from config import settings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI


def build_llm() -> BaseChatModel:
    """Gemini. Reads GOOGLE_API_KEY from the environment."""
    return ChatGoogleGenerativeAI(model=settings.gemini_model, temperature=settings.llm_temperature)
