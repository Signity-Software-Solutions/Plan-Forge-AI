"""LLM provider config + live connection test (drives the Studio AI panel)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.domain.interfaces.ports import LLMRequest
from app.infrastructure.llm.providers import LLMProviderFactory
from app.infrastructure.llm.runtime_config import (
    available_providers,
    get_runtime_llm,
    provider_details,
    set_runtime_llm,
)

router = APIRouter(prefix="/llm", tags=["llm"])


class LLMConfigIn(BaseModel):
    provider: str
    normalize_mode: str = "llm_labels_only"


@router.get("/config")
async def get_config() -> dict:
    rt = get_runtime_llm()
    return {
        "active_provider": rt["provider"],
        "normalize_mode": rt["normalize_mode"],
        "available": available_providers(),
        "details": provider_details(),
    }


@router.post("/config")
async def set_config(body: LLMConfigIn) -> dict:
    rt = set_runtime_llm(provider=body.provider, normalize_mode=body.normalize_mode)
    return {"ok": True, "active_provider": rt["provider"], "normalize_mode": rt["normalize_mode"]}


@router.post("/test")
async def test_connection() -> dict:
    cfg = get_settings()
    provider = get_runtime_llm()["provider"]
    if provider in ("none", "", "env"):
        return {"ok": False, "provider": provider, "error": "No LLM provider selected (heuristic only)."}
    try:
        client = LLMProviderFactory.create(provider, cfg)
        result = await client.complete(
            LLMRequest(
                system_prompt="You are a health check. Reply with the single word: OK.",
                user_prompt="ping",
                temperature=0.0,
                max_tokens=16,
            )
        )
        return {
            "ok": True,
            "provider": provider,
            "model": result.model,
            "response": (result.content or "").strip()[:200],
        }
    except Exception as exc:  # surface provider/config errors to the UI
        return {"ok": False, "provider": provider, "error": str(exc)[:400]}
