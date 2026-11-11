"""Read persisted layout artifacts when in-memory state was cleared (e.g. after uvicorn restart)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def layout_json_path(project_id: str) -> Path:
    return get_settings().output_dir / project_id / "layout.json"


def latest_svg_path(project_id: str) -> Path | None:
    d = get_settings().output_dir / project_id
    if not d.is_dir():
        return None
    svgs = list(d.glob("layout_*.svg"))
    if not svgs:
        return None
    return max(svgs, key=lambda p: p.stat().st_mtime)


def latest_dxf_path(project_id: str) -> Path | None:
    d = get_settings().output_dir / project_id
    if not d.is_dir():
        return None
    dxfs = list(d.glob("layout_*.dxf"))
    if not dxfs:
        return None
    return max(dxfs, key=lambda p: p.stat().st_mtime)


def load_layout_json_from_disk(project_id: str) -> dict | None:
    p = layout_json_path(project_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read %s: %s", p, e)
        return None
