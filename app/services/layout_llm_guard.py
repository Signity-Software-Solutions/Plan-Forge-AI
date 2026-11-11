"""Structural gates: compare LLM-normalized layout to heuristic baseline (image/CAD)."""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

from app.models.layout_schema import UnifiedLayout
from app.utils.validators import validate_layout, validation_errors_only

# Reject LLM output if wall count drops more than this fraction vs baseline.
_WALL_COUNT_DROP_MAX = 0.15
# Reject if candidate axis-aligned bbox area shrinks more than this vs baseline.
_BBOX_AREA_SHRINK_MAX = 0.30
# Reject if candidate bbox diagonal shrinks more than this vs baseline.
_BBOX_DIAG_SHRINK_MAX = 0.30
# Max centroid drift per matched room (index order), as fraction of baseline bbox diagonal.
_ROOM_CENTROID_DRIFT_MAX = 0.20


def _collect_points(layout: UnifiedLayout) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for w in layout.walls:
        pts.append((w.x1, w.y1))
        pts.append((w.x2, w.y2))
    for r in layout.rooms:
        for p in r.polygon:
            if len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
    for f in layout.fixtures:
        pts.append((f.x, f.y))
    for o in layout.openings:
        pts.extend([(o.x1, o.y1), (o.x2, o.y2)])
    return pts


def _bbox_stats(pts: list[tuple[float, float]]) -> tuple[float, float, float, float, float, float]:
    """minx, miny, maxx, maxy, diagonal, area (axis-aligned)."""
    if not pts:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = maxx - minx
    h = maxy - miny
    diag = math.hypot(w, h)
    area = max(w * h, 0.0)
    return (minx, miny, maxx, maxy, diag, area)


def _room_centroid(polygon: list[list[float]]) -> tuple[float, float] | None:
    if len(polygon) < 3:
        return None
    sx = sy = 0.0
    n = 0
    for p in polygon:
        if len(p) >= 2:
            sx += float(p[0])
            sy += float(p[1])
            n += 1
    if n == 0:
        return None
    return (sx / n, sy / n)


def gates_llm_vs_baseline(candidate: UnifiedLayout, baseline: UnifiedLayout) -> tuple[bool, str]:
    """
    Return (True, "") if candidate is safe to keep vs baseline.
    Otherwise (False, short_reason_code).
    """
    crit = validation_errors_only(candidate)
    if crit:
        return False, f"validation_{crit[0].code}"

    hard_codes = frozenset(
        {"room_polygon", "room_not_closed", "invalid_polygon", "room_geometry", "degenerate_wall"}
    )
    for iss in validate_layout(candidate):
        if iss.code in hard_codes:
            return False, f"validation_{iss.code}"

    b_pts = _collect_points(baseline)
    c_pts = _collect_points(candidate)
    _, _, _, _, b_diag, b_area = _bbox_stats(b_pts)
    _, _, _, _, c_diag, c_area = _bbox_stats(c_pts)

    # No geometry in baseline — only schema checks apply.
    if not b_pts:
        return True, ""

    nw_b, nw_c = len(baseline.walls), len(candidate.walls)
    if nw_b >= 5 and nw_c < int(math.ceil(nw_b * (1.0 - _WALL_COUNT_DROP_MAX))):
        return False, "wall_count_drop"
    if 1 <= nw_b < 5 and nw_c < max(0, nw_b - 1):
        return False, "wall_count_drop"

    nr_b = len([r for r in baseline.rooms if len(r.polygon) >= 3])
    nr_c = len([r for r in candidate.rooms if len(r.polygon) >= 3])
    if nr_b >= 3 and nr_c < int(math.ceil(nr_b * (1.0 - _WALL_COUNT_DROP_MAX))):
        return False, "room_count_drop"

    if b_diag > 1e-6 and c_diag < b_diag * (1.0 - _BBOX_DIAG_SHRINK_MAX):
        return False, "bbox_diag_shrink"

    if b_area > 1e-6 and c_area < b_area * (1.0 - _BBOX_AREA_SHRINK_MAX):
        return False, "bbox_area_shrink"

    # Centroid drift by index for rooms with valid polygons on both sides.
    if b_diag > 1e-6:
        max_drift = b_diag * _ROOM_CENTROID_DRIFT_MAX
        for i, br in enumerate(baseline.rooms):
            if i >= len(candidate.rooms):
                break
            cb = _room_centroid(br.polygon)
            cc = _room_centroid(candidate.rooms[i].polygon)
            if cb is None or cc is None:
                continue
            if math.hypot(cc[0] - cb[0], cc[1] - cb[1]) > max_drift:
                return False, "room_centroid_drift"

    return True, ""


def accept_llm_or_fallback(
    candidate: UnifiedLayout,
    baseline: UnifiedLayout,
    *,
    source: str,
) -> UnifiedLayout:
    """
    If gates pass, return candidate with meta. Else return baseline with rejection meta.
    ``source`` is e.g. ``ollama`` or ``http_llm`` for successful path metadata.
    """
    ok, reason = gates_llm_vs_baseline(candidate, baseline)
    if ok:
        meta = dict(candidate.meta)
        meta["normalized_by"] = source
        meta["llm_gates_passed"] = True
        meta.pop("llm_candidate_rejected", None)
        meta.pop("rejection_reason", None)
        return candidate.model_copy(update={"meta": meta})
    meta_b = dict(baseline.meta)
    meta_b["normalized_by"] = "heuristic_llm_rejected"
    meta_b["llm_candidate_rejected"] = True
    meta_b["rejection_reason"] = reason
    meta_b["llm_rejection_source"] = source
    logger.info("LLM layout rejected by gates (%s); using heuristic baseline.", reason)
    return baseline.model_copy(update={"meta": meta_b})


def merge_sheet_label_patches(baseline: UnifiedLayout, patches: dict[str, Any]) -> UnifiedLayout:
    """
    Apply label-only LLM output onto baseline (coordinates unchanged).

    Expected ``patches`` keys (all optional):
    - ``room_names``: list of {``index``: int, ``name``: str}
    - ``opening_labels``: list of {``index``: int, ``label``: str}
    - ``fixture_labels``: list of {``index``: int, ``label``: str | null}
    - ``meta_patches``: dict merged into ``meta`` (string values only where safe)
    """
    rooms = list(baseline.rooms)
    for item in patches.get("room_names") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            name = str(item.get("name") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(rooms) and name:
            r = rooms[idx]
            rooms[idx] = r.model_copy(update={"name": name})

    openings = list(baseline.openings)
    for item in patches.get("opening_labels") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            lab = item.get("label")
            lab_s = None if lab is None else str(lab)[:200]
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(openings):
            o = openings[idx]
            openings[idx] = o.model_copy(update={"label": lab_s})

    fixtures = list(baseline.fixtures)
    for item in patches.get("fixture_labels") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            lab = item.get("label")
            lab_s = None if lab is None else str(lab)[:200]
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(fixtures):
            f = fixtures[idx]
            fixtures[idx] = f.model_copy(update={"label": lab_s})

    meta = dict(baseline.meta)
    mp = patches.get("meta_patches")
    if isinstance(mp, dict):
        for k, v in mp.items():
            if isinstance(k, str) and len(k) <= 120:
                if isinstance(v, (str, int, float, bool)) or v is None:
                    meta[k] = v
    meta["normalized_by"] = "heuristic_llm_labels"
    meta["llm_labels_only"] = True

    return baseline.model_copy(update={"rooms": rooms, "openings": openings, "fixtures": fixtures, "meta": meta})
