"""Runtime LLM configuration — GET to read, POST to switch provider. Resets on server restart."""

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import state
from app.core.config import get_settings
from app.core.llm_client import _effective_llm_params

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

_VALID_PROVIDERS = ("env", "none", "ollama", "openai", "gemini")
_VALID_MODES = ("llm_labels_only", "llm_merge")


class LLMProviderIn(BaseModel):
    provider: str
    normalize_mode: str = "llm_labels_only"


def _available_providers(settings) -> dict[str, bool]:
    """Which providers have credentials configured in .env."""
    return {
        "ollama": bool((settings.ollama_host or "").strip() and (settings.ollama_model or "").strip()),
        "openai": bool((settings.openai_api_key or "").strip()),
        "gemini": bool((settings.gemini_api_key or "").strip()),
    }


def _provider_detail(settings, provider: str) -> dict[str, str]:
    """Human-readable detail for a configured provider."""
    if provider == "ollama":
        return {"model": (settings.ollama_model or "").strip(), "host": (settings.ollama_host or "").strip()}
    if provider == "openai":
        return {"model": (settings.openai_model or "gpt-4o").strip(), "host": "api.openai.com"}
    if provider == "gemini":
        return {"model": (settings.gemini_model or "gemini-2.0-flash").strip(), "host": "generativelanguage.googleapis.com"}
    return {}


@router.get("/api/llm-config")
def get_llm_config() -> dict[str, Any]:
    """Return active provider, available providers, and their details."""
    settings = get_settings()
    available = _available_providers(settings)
    active = (state.runtime_llm.get("provider") or "env").strip().lower()
    nm = (state.runtime_llm.get("normalize_mode") or settings.planforge_sheet_normalize_mode or "heuristic_only").strip().lower()

    details = {}
    for p, avail in available.items():
        if avail:
            details[p] = _provider_detail(settings, p)

    return {
        "active_provider": active,
        "normalize_mode": nm,
        "available": available,
        "details": details,
    }


@router.post("/api/llm-config")
def set_llm_provider(body: LLMProviderIn) -> dict[str, Any]:
    """Switch the active LLM provider. Credentials are read from .env."""
    provider = (body.provider or "").strip().lower()
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(status_code=422, detail=f"provider must be one of {_VALID_PROVIDERS}")
    nm = (body.normalize_mode or "llm_labels_only").strip().lower()
    if nm not in _VALID_MODES:
        raise HTTPException(status_code=422, detail=f"normalize_mode must be one of {_VALID_MODES}")

    # Verify the selected provider is actually configured (skip check for env/none)
    if provider not in ("env", "none"):
        settings = get_settings()
        available = _available_providers(settings)
        if not available.get(provider):
            raise HTTPException(
                status_code=422,
                detail=f"Provider '{provider}' is not configured in .env. Add the required key(s) and restart.",
            )

    state.runtime_llm["provider"] = provider
    state.runtime_llm["normalize_mode"] = nm
    return {"ok": True, "active_provider": provider, "normalize_mode": nm}


_TEST_PROMPT = 'Respond with only this JSON and nothing else: {"ok": true}'


@router.post("/api/llm-test")
def test_llm_connection() -> dict[str, Any]:
    """Make a real call to the active LLM provider to confirm it is reachable and responding."""
    settings = get_settings()
    provider = (state.runtime_llm.get("provider") or "env").strip().lower()

    if provider == "none":
        return {"ok": False, "provider": "none", "error": "No AI provider selected — choose Ollama, OpenAI, or Gemini first."}

    host, model, http_url, http_key, http_model, _ = _effective_llm_params(settings, state.runtime_llm)

    # ── Ollama ────────────────────────────────────────────────────────────────
    if host and model:
        try:
            import ollama
            client = ollama.Client(host=host.rstrip("/"))
            resp = client.chat(
                model=model,
                messages=[{"role": "user", "content": _TEST_PROMPT}],
                options={"temperature": 0},
            )
            raw = (resp["message"]["content"] or "").strip()
            logger.info("LLM test OK — ollama model=%s raw=%r", model, raw[:80])
            return {
                "ok": True,
                "provider": "ollama",
                "model": model,
                "host": host,
                "response": raw[:200],
            }
        except Exception as exc:
            logger.warning("LLM test failed — ollama: %s", exc)
            return {"ok": False, "provider": "ollama", "model": model, "host": host, "error": str(exc)}

    # ── OpenAI / Gemini (OpenAI-compatible HTTP) ──────────────────────────────
    if http_url and http_key and http_model:
        url_full = http_url.rstrip("/") + "/chat/completions"
        body = {
            "model": http_model,
            "temperature": 0,
            "max_tokens": 30,
            "messages": [{"role": "user", "content": _TEST_PROMPT}],
        }
        headers = {"Authorization": f"Bearer {http_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=20.0) as client:
                r = client.post(url_full, json=body, headers=headers)

            # Always capture body before raise_for_status so we can show it on error
            try:
                resp_json = r.json()
            except Exception:
                resp_json = None
            resp_text = r.text[:400]

            raw_body = resp_text  # always available for debugging

            if r.status_code >= 400:
                err_msg = f"HTTP {r.status_code}"
                if isinstance(resp_json, dict):
                    inner = resp_json.get("error") or {}
                    if isinstance(inner, dict):
                        detail_msg = inner.get("message") or inner.get("status") or ""
                    else:
                        detail_msg = str(inner)
                    err_msg += f" — {detail_msg[:300]}" if detail_msg else f" — {json.dumps(resp_json)[:300]}"
                elif resp_json is not None:
                    err_msg += f" — {json.dumps(resp_json)[:300]}"
                else:
                    err_msg += f" — {raw_body}"
                logger.warning("LLM test failed — %s %s: %s", provider, url_full, err_msg)
                return {"ok": False, "provider": provider, "model": http_model, "url": url_full, "error": err_msg, "raw": raw_body}

            # Parse success response — handle both dict and unexpected shapes
            content = ""
            used_model = http_model
            if isinstance(resp_json, dict):
                try:
                    content = str(resp_json["choices"][0]["message"]["content"] or "").strip()
                except (KeyError, IndexError, TypeError):
                    content = f"[unexpected response shape] {raw_body[:200]}"
                used_model = resp_json.get("model") or http_model
            else:
                content = f"[non-dict response] {raw_body[:200]}"

            logger.info("LLM test OK — %s model=%s raw=%r", provider, used_model, content[:80])
            return {
                "ok": True,
                "provider": provider,
                "model": used_model,
                "url": url_full,
                "response": content[:200],
            }
        except Exception as exc:
            logger.warning("LLM test failed — %s: %s", provider, exc)
            return {"ok": False, "provider": provider, "model": http_model, "url": url_full, "error": str(exc)}

    return {
        "ok": False,
        "provider": provider,
        "error": "Provider selected but credentials not found in .env. Add the API key and restart the server.",
    }
