"""The LLM, picked in .env with LLM_BACKEND, as in the other examples."""

import os


def build_llm():
    backend = os.getenv("LLM_BACKEND", "ollama").lower()
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))
    if backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=os.getenv("OLLAMA_MODEL", "llama3.1"),
            base_url=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"),
            temperature=temperature,
        )
    if backend == "openai":
        from langchain_openai import ChatOpenAI  # reads OPENAI_API_KEY

        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            temperature=temperature,
        )
    if backend == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # reads GOOGLE_API_KEY

        return ChatGoogleGenerativeAI(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"), temperature=temperature
        )
    raise ValueError(f"Unsupported LLM_BACKEND: {backend}")
