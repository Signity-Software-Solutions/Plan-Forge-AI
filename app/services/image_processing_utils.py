"""Phase 1–2 image enhancements: wall segment merging and contour-based room detection."""

from __future__ import annotations

import base64
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from shapely.geometry import GeometryCollection, LineString, MultiLineString
from shapely.ops import linemerge


def _segment_angle_deg(s: dict[str, float]) -> float:
    dx = s["x2"] - s["x1"]
    dy = s["y2"] - s["y1"]
    return math.degrees(math.atan2(dy, dx)) % 180.0


def _min_endpoint_distance(a: dict[str, float], b: dict[str, float]) -> float:
    pa = [(a["x1"], a["y1"]), (a["x2"], a["y2"])]
    pb = [(b["x1"], b["y1"]), (b["x2"], b["y2"])]
    best = float("inf")
    for xa, ya in pa:
        for xb, yb in pb:
            d = math.hypot(xa - xb, ya - yb)
            if d < best:
                best = d
    return best


def _angle_close(a: float, b: float, tol: float) -> bool:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d) <= tol


class _UF:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        if self.p[x] != x:
            self.p[x] = self.find(self.p[x])
        return self.p[x]

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def merge_hough_segments(
    segments: list[dict[str, float]],
    *,
    angle_tol_deg: float = 5.0,
    endpoint_cluster_px: float = 10.0,
    gap_merge_px: float = 5.0,
) -> list[dict[str, float]]:
    """Phase 1: cluster by angle + endpoint proximity, merge collinear chains with gap tolerance."""
    if not segments:
        return []
    n = len(segments)
    angles = [_segment_angle_deg(s) for s in segments]
    uf = _UF(n)
    for i in range(n):
        for j in range(i + 1, n):
            if not _angle_close(angles[i], angles[j], angle_tol_deg):
                continue
            if _min_endpoint_distance(segments[i], segments[j]) <= endpoint_cluster_px:
                uf.union(i, j)

    clusters: dict[int, list[dict[str, float]]] = {}
    for i in range(n):
        r = uf.find(i)
        clusters.setdefault(r, []).append(segments[i])

    out: list[dict[str, float]] = []
    for _root, segs in clusters.items():
        out.extend(_merge_collinear_cluster(segs, gap_merge_px))
    return out


def _merge_collinear_cluster(segs: list[dict[str, float]], gap_tol: float) -> list[dict[str, float]]:
    if not segs:
        return []
    ext = gap_tol / 2.0
    lines: list[LineString] = []
    for s in segs:
        x1, y1, x2, y2 = s["x1"], s["y1"], s["x2"], s["y2"]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        if ln < 1e-6:
            continue
        ux, uy = dx / ln, dy / ln
        lines.append(
            LineString(
                [
                    (x1 - ux * ext, y1 - uy * ext),
                    (x2 + ux * ext, y2 + uy * ext),
                ]
            )
        )
    if not lines:
        return []
    merged = linemerge(lines)
    result: list[dict[str, float]] = []

    def emit_linestring(ls: LineString) -> None:
        coords = list(ls.coords)
        for i in range(len(coords) - 1):
            x1, y1 = coords[i]
            x2, y2 = coords[i + 1]
            if math.hypot(x2 - x1, y2 - y1) >= 0.5:
                result.append({"x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2)})

    if isinstance(merged, LineString):
        emit_linestring(merged)
    elif isinstance(merged, MultiLineString):
        for g in merged.geoms:
            emit_linestring(g)
    elif isinstance(merged, GeometryCollection):
        for g in merged.geoms:
            if isinstance(g, LineString):
                emit_linestring(g)
            elif isinstance(g, MultiLineString):
                for h in g.geoms:
                    emit_linestring(h)
    else:
        geoms = getattr(merged, "geoms", [])
        for g in geoms:
            if isinstance(g, LineString):
                emit_linestring(g)
            elif isinstance(g, MultiLineString):
                for h in g.geoms:
                    emit_linestring(h)

    return result if result else segs


def detect_rooms_from_contours(
    gray: np.ndarray,
    labels: list[dict[str, Any]],
    *,
    min_area_px: float = 5000.0,
    max_area_fraction: float = 0.92,
    approx_eps_ratio: float = 0.002,
) -> list[dict[str, Any]]:
    """Phase 2: binary contours → room polygons + names from nearest OCR centroid."""
    h, w = gray.shape[:2]
    img_area = float(w * h)
    max_area = max_area_fraction * img_area

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = np.ones((5, 5), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    rooms: list[dict[str, Any]] = []
    for cnt in contours:
        area = float(cv2.contourArea(cnt))
        if area < min_area_px or area > max_area:
            continue
        peri = cv2.arcLength(cnt, True)
        eps = approx_eps_ratio * peri
        approx = cv2.approxPolyDP(cnt, eps, True)
        if len(approx) < 3:
            continue
        poly: list[list[float]] = []
        for pt in approx:
            poly.append([float(pt[0][0]), float(pt[0][1])])
        M = cv2.moments(cnt)
        if M["m00"] == 0:
            continue
        cx = float(M["m10"] / M["m00"])
        cy = float(M["m01"] / M["m00"])

        name = _nearest_ocr_label(cx, cy, labels, max_dist_px=max(w, h) * 0.25)

        rooms.append(
            {
                "name": name,
                "area": area,
                "polygon": poly,
            }
        )

    return rooms


def _nearest_ocr_label(cx: float, cy: float, labels: list[dict[str, Any]], max_dist_px: float) -> str:
    best: str = ""
    best_d = float("inf")
    for lb in labels:
        lx = float(lb["x"]) + float(lb["w"]) / 2.0
        ly = float(lb["y"]) + float(lb["h"]) / 2.0
        d = math.hypot(cx - lx, cy - ly)
        if d < best_d and d <= max_dist_px:
            best_d = d
            best = str(lb.get("text") or "").strip()
    return best


def png_base64_data_uri(path: Path) -> tuple[str, int, int]:
    """Encode the image file as a PNG data URI; returns (data_uri, width, height)."""
    img_bgr = cv2.imread(str(path))
    if img_bgr is None:
        raise ValueError(f"Could not read image: {path}")
    ih, iw = img_bgr.shape[:2]
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        raise ValueError("cv2.imencode failed")
    b64 = base64.standard_b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/png;base64,{b64}", iw, ih
