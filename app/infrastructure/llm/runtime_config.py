"""In-process runtime LLM selection for the v1 Studio UI.

Mirrors the MVP's runtime provider switch but scoped to the v1 app. The parse
use case reads ``get_runtime_llm()`` and passes it as ``llm_override`` so the UI
can change provider/mode without a restart. Resets on process restart.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings

_runtime: dict[str, str] = {"provider": "none", "normalize_mode": "llm_labels_only"}


def get_runtime_llm() -> dict[str, str]:
    return dict(_runtime)


def set_runtime_llm(*, provider: str, normalize_mode: str) -> dict[str, str]:
    _runtime["provider"] = provider
    _runtime["normalize_mode"] = normalize_mode
    return get_runtime_llm()


def available_providers(settings: Settings | None = None) -> dict[str, bool]:
    cfg = settings or get_settings()
    return {
        "ollama": bool(cfg.ollama_host and cfg.ollama_model),
        "openai": bool(cfg.openai_api_key),
        "gemini": bool(cfg.gemini_api_key),
        "azure_openai": bool(cfg.azure_openai_endpoint and cfg.azure_openai_api_key),
        "openrouter": bool(cfg.openrouter_api_key),
    }


def provider_details(settings: Settings | None = None) -> dict[str, dict]:
    cfg = settings or get_settings()
    return {
        "ollama": {"host": cfg.ollama_host, "model": cfg.ollama_model},
        "openai": {"model": cfg.openai_model},
        "gemini": {"model": cfg.gemini_model},
        "azure_openai": {"model": cfg.azure_openai_deployment},
        "openrouter": {"model": cfg.openrouter_model},
    }
