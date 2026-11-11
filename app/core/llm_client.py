"""LLM used sparingly: Ollama (OLLAMA_HOST) or HTTP API or heuristic; gates preserve geometry."""

import json
import logging
import re
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.layout_schema import UnifiedLayout
from app.services.layout_llm_guard import accept_llm_or_fallback, merge_sheet_label_patches

logger = logging.getLogger(__name__)

# ── LLM tuning constants ─────────────────────────────────────────────────────
_LLM_MAX_INPUT_CHARS = 120_000   # truncate payload before sending to any LLM
_TEMP_OLLAMA = 0                 # deterministic for structured JSON output
_TEMP_HTTP_LABELS = 0.1          # slight variance for label improvement
_TEMP_HTTP_FULL = 0.2            # slightly more for full layout rewrite

SYSTEM_PROMPT = """You are a layout normalization engine.
Input may include CAD extracted JSON, OCR text, and dimension sheet data.
Output MUST be a single JSON object matching this shape exactly:
{
  "units": "mm",
  "walls": [{"x1": number, "y1": number, "x2": number, "y2": number}],
  "rooms": [{"name": string, "area": number or null, "polygon": [[x,y], ...]}],
  "fixtures": [{"kind": string, "x": number, "y": number, "label": string or null}],
  "openings": [{"kind": string, "x1": number, "y1": number, "x2": number, "y2": number, "label": string or null}],
  "meta": object (optional)
}
Resolve naming inconsistencies; infer missing dimensions only when reasonably justified.
Use consistent numeric units as given in input.units when present; default mm.
"""

OLLAMA_SYSTEM_PROMPT = (
    "You are a JSON API. You must respond with only a valid JSON object. "
    "No explanation. No markdown. No code blocks. Just the raw JSON object."
)

OLLAMA_SYSTEM_LABELS = (
    "You are a JSON API. Respond with only a JSON object with optional keys: "
    "room_names, opening_labels, fixture_labels, meta_patches. "
    "room_names: [{\"index\": number, \"name\": string}]. "
    "opening_labels: [{\"index\": number, \"label\": string|null}]. "
    "fixture_labels: [{\"index\": number, \"label\": string|null}]. "
    "meta_patches: object with string/number/boolean values only. "
    "Do not include coordinates or geometry."
)

SHEET_LLM_MERGE_INSTRUCTION = """Return a single JSON object with keys: units, walls, rooms, fixtures, openings, meta.
CRITICAL: Preserve every wall endpoint, room polygon vertex, fixture (x,y), and opening segment from the input JSON exactly (same numeric values). You may only adjust human-readable string fields (room names, labels) and add non-destructive meta keys.
Column name hints: "Item Description" or "Description" or "Item" = room name. "Dimension" may contain combined LxW values like "600x600mm" or "1200x600" — split into length and width in mm.
Input data:
"""


def _ollama_instruction_full_geom(file_type: str, combined: dict[str, Any]) -> str:
    parsed = combined.get("parsed") if isinstance(combined.get("parsed"), dict) else {}
    units = parsed.get("units") or combined.get("units") or "mm"
    units = str(units)[:16]
    ft = str(file_type or "image")
    return (
        f'Return a single JSON object with keys: units, walls, rooms, fixtures, openings, meta.\n'
        f'"units" must be "{units}" unless the input clearly states another canonical unit (mm, m, ft, in).\n'
        "Preserve walls, room polygons, fixture positions, and openings from the input when they are present; "
        "do not return empty fixtures or openings unless the input truly has none.\n"
        "You may normalize room names, fix obvious duplicate walls, and enrich meta.\n"
        "Column name hints: 'Item Description', 'Description', or 'Item' = room/space name. "
        "'Dimension' may contain combined LxW values like '600x600mm' or '1200x600' — split into length and width in mm.\n"
        f"Input file_type: {ft}\n"
        "Data to normalize:\n"
    )


def _unwrap_heuristic_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten `{ "parsed": <parser_json>, "file_type": ... }` from /normalize pipeline."""
    parsed = raw.get("parsed")
    if not isinstance(parsed, dict):
        return raw
    if not any(
        k in parsed for k in ("walls", "rooms", "labels", "source", "dimensions", "units")
    ):
        return raw
    out = {**parsed}
    meta = dict(out.get("meta") or {})
    for k in ("file_type", "filename"):
        if k in raw:
            meta[k] = raw[k]
    out["meta"] = meta
    if raw.get("units") and not out.get("units"):
        out["units"] = raw["units"]
    return out


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if present."""
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n?", t)
    if fence:
        t = t[fence.end() :]
    t = t.strip()
    if t.endswith("```"):
        t = t[: -3].rstrip()
    return t.strip()


def _layout_json_from_llm_text(content: str) -> dict[str, Any]:
    """Parse JSON from model output; strip fences; optionally brace-slice."""
    text = _strip_markdown_fences(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def heuristic_normalize(raw: dict[str, Any]) -> UnifiedLayout:
    """No-LLM path: coerce parser output toward UnifiedLayout."""
    raw = _unwrap_heuristic_input(raw)
    units = str(raw.get("units") or "mm")
    walls_in = raw.get("walls") or []
    walls = []
    for w in walls_in:
        if isinstance(w, dict) and all(k in w for k in ("x1", "y1", "x2", "y2")):
            walls.append(
                {
                    "x1": float(w["x1"]),
                    "y1": float(w["y1"]),
                    "x2": float(w["x2"]),
                    "y2": float(w["y2"]),
                }
            )

    rooms_in = raw.get("rooms") or []
    rooms = []
    for r in rooms_in:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "")
        poly = r.get("polygon") or []
        cleaned_poly: list[list[float]] = []
        for pt in poly:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                cleaned_poly.append([float(pt[0]), float(pt[1])])
        area = r.get("area")
        rooms.append(
            {
                "name": name,
                "area": float(area) if area is not None else None,
                "polygon": cleaned_poly,
            }
        )

    def _opt_float(v: Any) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    fixtures = []
    for f in raw.get("fixtures") or []:
        if isinstance(f, dict):
            fixtures.append(
                {
                    "kind": str(f.get("kind") or "generic"),
                    "x": float(f.get("x", 0)),
                    "y": float(f.get("y", 0)),
                    "label": f.get("label"),
                    "width_mm": _opt_float(f.get("width_mm")),
                    "depth_mm": _opt_float(f.get("depth_mm")),
                    "height_mm": _opt_float(f.get("height_mm")),
                    "rotation_deg": float(f.get("rotation_deg") or 0),
                }
            )

    openings = []
    for o in raw.get("openings") or []:
        if isinstance(o, dict):
            openings.append(
                {
                    "kind": str(o.get("kind") or "door"),
                    "x1": float(o.get("x1", 0)),
                    "y1": float(o.get("y1", 0)),
                    "x2": float(o.get("x2", 0)),
                    "y2": float(o.get("y2", 0)),
                    "label": o.get("label"),
                }
            )

    meta = dict(raw.get("meta") or {})
    meta["normalized_by"] = "heuristic"

    return UnifiedLayout(
        units=units,
        walls=walls,
        rooms=rooms,
        fixtures=fixtures,
        openings=openings,
        meta=meta,
    )


def _normalize_once_ollama(
    combined_payload: dict[str, Any],
    host: str,
    model: str,
    *,
    user_instruction: str,
) -> UnifiedLayout:
    """Single Ollama chat call; raises on failure."""
    import ollama

    client = ollama.Client(host=host.rstrip("/"))
    parsed_layout = combined_payload.get("parsed")
    if not isinstance(parsed_layout, dict):
        parsed_layout = combined_payload
    layout_json = json.dumps(parsed_layout, default=str)
    user_content = (user_instruction + layout_json)[:_LLM_MAX_INPUT_CHARS]
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        options={"temperature": _TEMP_OLLAMA},
    )
    raw_text = response["message"]["content"] or ""
    logger.info("Ollama raw response (prefix): %s", raw_text[:500])
    parsed = _layout_json_from_llm_text(raw_text)
    return UnifiedLayout.model_validate(parsed)


def _ollama_sheet_labels_only(baseline: UnifiedLayout, host: str, model: str) -> UnifiedLayout:
    import ollama

    client = ollama.Client(host=host.rstrip("/"))
    summary = {
        "task": "improve_labels_only",
        "rooms": [{"index": i, "name": r.name} for i, r in enumerate(baseline.rooms)],
        "openings": [{"index": i, "label": o.label} for i, o in enumerate(baseline.openings)],
        "fixtures": [{"index": i, "label": f.label} for i, f in enumerate(baseline.fixtures)],
    }
    user_content = json.dumps(summary, default=str)[:_LLM_MAX_INPUT_CHARS]
    response = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": OLLAMA_SYSTEM_LABELS},
            {"role": "user", "content": user_content},
        ],
        options={"temperature": _TEMP_OLLAMA},
    )
    raw_text = response["message"]["content"] or ""
    patches = _layout_json_from_llm_text(raw_text)
    if not isinstance(patches, dict):
        raise ValueError("labels response is not a JSON object")
    return merge_sheet_label_patches(baseline, patches)


def _http_sheet_labels_only(baseline: UnifiedLayout, *, max_retries: int) -> UnifiedLayout | None:
    settings = get_settings()
    url = settings.llm_api_url
    key = settings.llm_api_key
    model = (settings.llm_model or "").strip()
    if not url or not key or not model:
        return None
    return _http_sheet_labels_only_explicit(
        baseline, url=url, key=key, model=model,
        timeout=settings.llm_timeout_seconds, max_retries=max_retries,
    )


def _http_sheet_labels_only_explicit(
    baseline: UnifiedLayout, *, url: str, key: str, model: str,
    timeout: float = 60.0, max_retries: int = 2,
) -> UnifiedLayout | None:
    summary = {
        "task": "improve_labels_only",
        "rooms": [{"index": i, "name": r.name} for i, r in enumerate(baseline.rooms)],
        "openings": [{"index": i, "label": o.label} for i, o in enumerate(baseline.openings)],
        "fixtures": [{"index": i, "label": f.label} for i, f in enumerate(baseline.fixtures)],
    }
    url_full = url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "temperature": _TEMP_HTTP_LABELS,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": OLLAMA_SYSTEM_LABELS},
            {"role": "user", "content": json.dumps(summary, default=str)[:_LLM_MAX_INPUT_CHARS]},
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url_full, json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
            content = data["choices"][0]["message"]["content"]
            patches = json.loads(content)
            if not isinstance(patches, dict):
                return None
            return merge_sheet_label_patches(baseline, patches)
        except Exception as e:
            last_err = e
            logger.warning("HTTP sheet labels attempt %s failed: %s", attempt + 1, e)
    logger.error("HTTP sheet labels failed: %s", last_err)
    return None


def _normalize_with_http_llm(
    combined_payload: dict[str, Any], *, max_retries: int
) -> UnifiedLayout | None:
    settings = get_settings()
    url = settings.llm_api_url
    key = settings.llm_api_key
    model = (settings.llm_model or "").strip()
    if not url or not key:
        return None
    if not model:
        logger.warning("LLM_API_URL and LLM_API_KEY are set but LLM_MODEL is empty; skipping HTTP LLM.")
        return None
    return _normalize_with_http_llm_explicit(
        combined_payload, url=url, key=key, model=model,
        timeout=settings.llm_timeout_seconds, max_retries=max_retries,
    )


def _normalize_with_http_llm_explicit(
    combined_payload: dict[str, Any], *, url: str, key: str, model: str,
    timeout: float = 60.0, max_retries: int = 2,
) -> UnifiedLayout | None:
    url_full = url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "temperature": _TEMP_HTTP_FULL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(combined_payload, default=str)[:_LLM_MAX_INPUT_CHARS]},
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url_full, json=body, headers=headers)
                r.raise_for_status()
                data = r.json()
            content = data["choices"][0]["message"]["content"]
            layout_json = json.loads(content)
            return UnifiedLayout.model_validate(layout_json)
        except Exception as e:
            last_err = e
            logger.warning("HTTP LLM normalize attempt %s failed: %s", attempt + 1, e)
    logger.error("HTTP LLM normalization failed; falling back: %s", last_err)
    return None


def _sheet_mode(settings: Any) -> str:
    raw = (getattr(settings, "planforge_sheet_normalize_mode", None) or "heuristic_only").strip().lower()
    if raw not in ("heuristic_only", "llm_merge", "llm_labels_only"):
        logger.warning("Invalid PLANFORGE_SHEET_NORMALIZE_MODE=%r; using heuristic_only", raw)
        return "heuristic_only"
    return raw


_VALID_MODES = ("heuristic_only", "llm_merge", "llm_labels_only")
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1/openai"
_OPENAI_BASE = "https://api.openai.com/v1"


def _effective_llm_params(
    settings: Any, override: dict | None
) -> tuple[str, str, str | None, str | None, str, str]:
    """Return (ollama_host, ollama_model, http_url, http_key, http_model, sheet_mode)."""
    provider = (override.get("provider") or "env").strip().lower() if override else "env"
    nm = (override.get("normalize_mode") or "").strip().lower() if override else ""
    if nm not in _VALID_MODES:
        nm = ""  # resolved below per provider

    if provider == "env":
        # Legacy: use whatever is in .env (ollama first, then generic http llm)
        return (
            (settings.ollama_host or "").strip(),
            (settings.ollama_model or "").strip(),
            settings.llm_api_url,
            settings.llm_api_key,
            (settings.llm_model or "").strip(),
            nm or _sheet_mode(settings),
        )

    if provider == "none":
        return ("", "", None, None, "", nm or "heuristic_only")

    if provider == "ollama":
        return (
            (settings.ollama_host or "").strip(),
            (settings.ollama_model or "").strip(),
            None, None, "",
            nm or _sheet_mode(settings),
        )

    if provider == "openai":
        return (
            "", "",
            _OPENAI_BASE,
            (settings.openai_api_key or "").strip() or None,
            (settings.openai_model or "gpt-4o").strip(),
            nm or _sheet_mode(settings),
        )

    if provider == "gemini":
        return (
            "", "",
            _GEMINI_BASE,
            (settings.gemini_api_key or "").strip() or None,
            (settings.gemini_model or "gemini-2.0-flash").strip(),
            nm or _sheet_mode(settings),
        )

    # unknown provider → heuristic
    logger.warning("Unknown LLM provider %r; using heuristic.", provider)
    return ("", "", None, None, "", nm or "heuristic_only")


def normalize_with_llm(
    combined_payload: dict[str, Any], *, max_retries: int = 2, llm_override: dict | None = None
) -> UnifiedLayout:
    """
    Sheets: default heuristic_only. Optional llm_labels_only / llm_merge (gated) via settings.
    Image/CAD: Ollama or HTTP full layout, then structural gates vs heuristic baseline; reject → heuristic.
    llm_override: runtime config dict from state.runtime_llm; if None or provider=="env", uses .env settings.
    """
    settings = get_settings()
    host, model, http_url, http_key, http_model, sheet_mode_val = _effective_llm_params(
        settings, llm_override
    )
    file_type = str(combined_payload.get("file_type") or "")

    if file_type == "sheet":
        baseline = heuristic_normalize(combined_payload)

        if sheet_mode_val == "heuristic_only":
            logger.info("Sheet normalize: heuristic only (mode=heuristic_only).")
            return baseline

        if sheet_mode_val == "llm_labels_only":
            # Guard: skip LLM when the labels summary would overflow the context window
            # or require a response too large to arrive within the timeout.
            # 500 items ≈ ~30 KB of JSON — well within one request; above that the
            # response itself (mirrors every label) would also exceed the limit.
            _total_items = len(baseline.rooms) + len(baseline.fixtures) + len(baseline.openings)
            if _total_items > 500:
                logger.info(
                    "Sheet labels-only: %d items exceeds LLM label limit (500); using heuristic.",
                    _total_items,
                )
                return baseline
            if host and model:
                try:
                    out = _ollama_sheet_labels_only(baseline, host, model)
                    logger.info("Sheet labels-only: applied Ollama patches.")
                    return out
                except Exception as e:
                    logger.warning("Ollama sheet labels-only failed: %s", e)
            if http_url and http_key and http_model:
                http_labels = _http_sheet_labels_only_explicit(
                    baseline, url=http_url, key=http_key, model=http_model,
                    timeout=settings.llm_timeout_seconds, max_retries=max_retries,
                )
                if http_labels is not None:
                    logger.info("Sheet labels-only: applied HTTP LLM patches.")
                    return http_labels
            logger.info("Sheet labels-only: no LLM result; using heuristic.")
            return baseline

        # llm_merge
        if host and model:
            try:
                logger.info("Sheet llm_merge: Ollama full layout with coordinate-preservation prompt.")
                cand = _normalize_once_ollama(
                    combined_payload, host, model,
                    user_instruction=SHEET_LLM_MERGE_INSTRUCTION,
                )
                return accept_llm_or_fallback(cand, baseline, source="ollama")
            except Exception as e:
                logger.warning("Ollama sheet merge failed: %s", e)
        if http_url and http_key and http_model:
            http_result = _normalize_with_http_llm_explicit(
                combined_payload, url=http_url, key=http_key, model=http_model,
                timeout=settings.llm_timeout_seconds, max_retries=max_retries,
            )
            if http_result is not None:
                return accept_llm_or_fallback(http_result, baseline, source="http_llm")

        logger.info("Sheet llm_merge: no LLM configured; using heuristic.")
        return baseline

    # --- image / cad ---
    baseline = heuristic_normalize(combined_payload)
    ft = file_type or "image"
    instruction = _ollama_instruction_full_geom(ft, combined_payload)

    if host and model:
        logger.info("Normalizing with Ollama at %s model=%s", host.rstrip("/"), model)
        last_ollama_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                layout = _normalize_once_ollama(
                    combined_payload, host, model, user_instruction=instruction
                )
                return accept_llm_or_fallback(layout, baseline, source="ollama")
            except Exception as e:
                last_ollama_err = e
                logger.warning("Ollama normalize attempt %s failed: %s", attempt + 1, e)
        logger.error("Ollama normalization failed after retries: %s", last_ollama_err)

    if http_url and http_key and http_model:
        http_result = _normalize_with_http_llm_explicit(
            combined_payload, url=http_url, key=http_key, model=http_model,
            timeout=settings.llm_timeout_seconds, max_retries=max_retries,
        )
        if http_result is not None:
            return accept_llm_or_fallback(http_result, baseline, source="http_llm")

    if not (host and model) and not (http_url and http_key and http_model):
        logger.info("No LLM backends configured; using heuristic normalization.")
    else:
        logger.info("All configured LLM backends failed or skipped; using heuristic normalization.")
    return baseline
