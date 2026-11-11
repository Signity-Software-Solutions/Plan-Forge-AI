"""Shapely + svgwrite: scale normalization, wall alignment sketch, room polygons → SVG."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import svgwrite
from shapely.geometry import LineString, MultiLineString, Polygon as SPolygon
from shapely.ops import unary_union

from app.models.layout_schema import Fixture, Opening, Room, UnifiedLayout, WallSegment
from app.services.sheet_parser import _LABEL_MAX_CHARS

# Distinct room fills for 2D plan SVG (muted architectural tones).
_ROOM_PALETTE = (
    "#e8eaed",
    "#eceff1",
    "#e3e6ea",
    "#eef1f4",
    "#e5e8ec",
    "#ebeff3",
    "#e2e5e9",
    "#f0f2f5",
)


def _room_fill(idx: int) -> str:
    return _ROOM_PALETTE[idx % len(_ROOM_PALETTE)]


def _room_visual(name: str, idx: int) -> tuple[str, bool]:
    """Fill colour and whether to overlay diagonal hatch (store / BOH)."""
    u = (name or "").upper()
    if "STORE" in u and "RETAIL" not in u:
        return "#dde6db", True
    if "RETAIL" in u or "SALES" in u:
        return "#e4e9f2", False
    if "ENTRY" in u or "LOBBY" in u:
        return "#e8ebe6", False
    if "AISLE" in u:
        return "#fafafa", False
    return _room_fill(idx), False


def _fixture_corners_mm(f: Fixture) -> list[tuple[float, float]] | None:
    w, d = f.width_mm, f.depth_mm
    if w is None or d is None or w <= 0 or d <= 0:
        return None
    rad = math.radians(f.rotation_deg or 0.0)
    c, s = math.cos(rad), math.sin(rad)
    cx, cy = f.x, f.y
    local = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
    out: list[tuple[float, float]] = []
    for lx, ly in local:
        rx = cx + c * lx - s * ly
        ry = cy + s * lx + c * ly
        out.append((rx, ry))
    return out


def _inset_quad(corners: list[tuple[float, float]], factor: float) -> list[tuple[float, float]]:
    cx = sum(p[0] for p in corners) / len(corners)
    cy = sum(p[1] for p in corners) / len(corners)
    return [(cx + (x - cx) * factor, cy + (y - cy) * factor) for x, y in corners]


def _fixture_palette(kind: str, label: str) -> tuple[str, str, bool]:
    k = (kind or "").lower()
    lab = (label or "").upper()
    accent = any(x in lab for x in ('"', "LFD", "SIGNAGE", "OLED", "NEO"))
    if k == "display":
        stroke = "#b91c1c" if accent else "#475569"
        return "#ffffff", stroke, True
    if k == "table":
        return "#ffffff", "#334155", False
    if k == "counter":
        return "#f1f5f9", "#525252", False
    if k == "shelving":
        return "#ffffff", "#404040", "ACCESSORIES" in lab
    if k == "chair":
        return "#f8fafc", "#64748b", False
    return "#f1f5f9", "#475569", False


def _translate_svg_dimensions_meta(meta: dict[str, Any], ox: float, oy: float) -> dict[str, Any]:
    """Shift dimension annotations when layout is normalized to origin."""
    out = dict(meta)
    dims = out.get("svg_dimensions")
    if not dims or not isinstance(dims, list):
        return out
    shifted: list[dict[str, Any]] = []
    for d in dims:
        if not isinstance(d, dict):
            continue
        shifted.append(
            {
                **d,
                "x1": float(d["x1"]) - ox,
                "y1": float(d["y1"]) - oy,
                "x2": float(d["x2"]) - ox,
                "y2": float(d["y2"]) - oy,
            }
        )
    out["svg_dimensions"] = shifted
    return out


def _draw_svg_dimensions(
    dwg: svgwrite.Drawing,
    layout_n: UnifiedLayout,
    tx,
    *,
    scale_mm_to_px: float,
    wall_px: float,
    font_px: float,
    with_background: bool,
) -> None:
    """Architectural-style mm labels from Excel (meta.svg_dimensions)."""
    dims = layout_n.meta.get("svg_dimensions") if layout_n.meta else None
    if not dims or not isinstance(dims, list):
        return

    offset_mm_default = float(layout_n.meta.get("svg_dimension_offset_mm") or 380.0)
    tick = max(4.0, wall_px * 0.55)
    stroke_dim = max(0.85, wall_px * 0.22)
    stroke_ext = max(0.55, wall_px * 0.15)
    col_dim_def = "#1d4ed8" if with_background else "#93c5fd"
    col_ext = "#64748b" if with_background else "#94a3b8"
    fs = max(9.5, min(font_px * 0.52, 18.0))
    fill_t_def = "#1e3a8a" if with_background else "#e2e8f0"
    stroke_t = "#ffffff" if with_background else "#0f172a"

    dim_g = dwg.add(dwg.g())
    for d in dims:
        if not isinstance(d, dict):
            continue
        try:
            x1, y1 = float(d["x1"]), float(d["y1"])
            x2, y2 = float(d["x2"]), float(d["y2"])
        except (KeyError, TypeError, ValueError):
            continue
        label = str(d.get("label") or "").strip()
        if not label:
            continue
        nx = float(d.get("nx") or 0.0)
        ny = float(d.get("ny") or 0.0)
        ln = math.hypot(nx, ny)
        if ln < 1e-9:
            continue
        nx, ny = nx / ln, ny / ln

        off_mm = float(d["offset_mm"]) if d.get("offset_mm") is not None else offset_mm_default
        col_dim = str(d.get("dim_stroke") or col_dim_def)
        fill_t = str(d.get("text_fill") or fill_t_def)

        sx1, sy1 = tx(x1, y1)
        sx2, sy2 = tx(x2, y2)
        dx1, dy1 = tx(x1 + nx * off_mm, y1 + ny * off_mm)
        dx2, dy2 = tx(x2 + nx * off_mm, y2 + ny * off_mm)

        dim_g.add(
            dwg.line(
                start=(sx1, sy1),
                end=(dx1, dy1),
                stroke=col_ext,
                stroke_width=stroke_ext,
                stroke_dasharray="4,3",
            )
        )
        dim_g.add(
            dwg.line(
                start=(sx2, sy2),
                end=(dx2, dy2),
                stroke=col_ext,
                stroke_width=stroke_ext,
                stroke_dasharray="4,3",
            )
        )
        dim_g.add(
            dwg.line(
                start=(dx1, dy1),
                end=(dx2, dy2),
                stroke=col_dim,
                stroke_width=stroke_dim,
            )
        )
        vx, vy = dx2 - dx1, dy2 - dy1
        vl = math.hypot(vx, vy)
        if vl < 1e-6:
            continue
        vx, vy = vx / vl, vy / vl
        px, py = -vy, vx
        ht = tick * 0.5
        dim_g.add(
            dwg.line(
                start=(dx1 - px * ht, dy1 - py * ht),
                end=(dx1 + px * ht, dy1 + py * ht),
                stroke=col_dim,
                stroke_width=stroke_dim,
            )
        )
        dim_g.add(
            dwg.line(
                start=(dx2 - px * ht, dy2 - py * ht),
                end=(dx2 + px * ht, dy2 + py * ht),
                stroke=col_dim,
                stroke_width=stroke_dim,
            )
        )

        mx, my = (dx1 + dx2) / 2.0, (dy1 + dy2) / 2.0
        ang = math.degrees(math.atan2(dy2 - dy1, dx2 - dx1))
        if ang > 90:
            ang -= 180
        if ang < -90:
            ang += 180

        dim_g.add(
            dwg.text(
                label,
                insert=(mx, my),
                text_anchor="middle",
                dominant_baseline="middle",
                fill=fill_t,
                stroke=stroke_t,
                stroke_width=max(2.0, fs * 0.14),
                font_size=f"{fs}px",
                font_family="'Segoe UI',system-ui,sans-serif",
                font_weight="600",
                transform=f"rotate({ang:.2f},{mx:.2f},{my:.2f})",
                style="paint-order: stroke fill;",
            )
        )


def _draw_fixtures_rich(
    dwg: svgwrite.Drawing,
    layout_n: UnifiedLayout,
    tx,
    *,
    wall_px: float,
    font_px: float,
) -> None:
    g = dwg.add(dwg.g())
    fs_small = max(5.0, min(font_px * 0.42, 12.0))
    r_fallback = max(3.2, wall_px * 1.0)
    for f in layout_n.fixtures:
        lab = (f.label or "")[:_LABEL_MAX_CHARS]
        kind = (f.kind or "").lower()
        fill_b, stroke_c, inner_scr = _fixture_palette(kind, lab)
        corners = _fixture_corners_mm(f)
        if corners and len(corners) >= 4:
            pts_svg = [tx(x, y) for x, y in corners]
            d_path = "M " + " L ".join(f"{p[0]:.2f},{p[1]:.2f}" for p in pts_svg) + " Z"
            g.add(
                dwg.path(
                    d=d_path,
                    fill=fill_b,
                    stroke=stroke_c,
                    stroke_width=max(0.75, wall_px * 0.32),
                    stroke_linejoin="round",
                )
            )
            if inner_scr and kind == "display":
                inner = _inset_quad(corners, 0.88)
                pts_i = [tx(x, y) for x, y in inner]
                d_i = "M " + " L ".join(f"{p[0]:.2f},{p[1]:.2f}" for p in pts_i) + " Z"
                g.add(dwg.path(d=d_i, fill="#1e293b", stroke="none"))
            elif inner_scr and kind == "shelving":
                xs = [p[0] for p in pts_svg]
                ys = [p[1] for p in pts_svg]
                x0, x1 = min(xs), max(xs)
                y0, y1 = min(ys), max(ys)
                band_h = max(2.0, (y1 - y0) * 0.3)
                g.add(
                    dwg.rect(
                        insert=(x0 + 1, y1 - band_h),
                        size=(max(1.0, x1 - x0 - 2), band_h),
                        fill="#cc0000",
                        opacity=0.65,
                        stroke="none",
                    )
                )
            cx, cy = f.x, f.y
            sx, sy = tx(cx, cy)
            g.add(
                dwg.text(
                    lab,
                    insert=(sx, sy),
                    text_anchor="middle",
                    dominant_baseline="middle",
                    font_size=f"{fs_small}px",
                    fill="#0f172a",
                    font_weight="bold",
                    font_family="Arial,sans-serif",
                )
            )
        else:
            cx, cy = tx(f.x, f.y)
            g.add(
                dwg.circle(
                    center=(cx, cy),
                    r=r_fallback,
                    fill=fill_b,
                    stroke=stroke_c,
                    stroke_width=max(0.6, wall_px * 0.28),
                )
            )
            if lab:
                g.add(
                    dwg.text(
                        lab,
                        insert=(cx, cy),
                        text_anchor="middle",
                        dominant_baseline="middle",
                        font_size=f"{fs_small * 0.88}px",
                        fill="#0f172a",
                        font_family="Arial,sans-serif",
                    )
                )


# Minimum inset from SVG edge to geometry (px); keeps breathing room on every side.
_MIN_LAYOUT_BORDER_PX = 52.0
# Extra uniform margin inside the SVG viewport (edge → background / content).
_LAYOUT_SVG_OUTER_GUTTER_PX = 14.0


def _title_block_canvas_reserve(
    layout_n: UnifiedLayout,
    cw0: float,
    ch0: float,
    padding: float,
) -> tuple[float, float]:
    """Extra width/height so the bottom-right title block sits outside the scaled plan (same sizing as _draw_title_block)."""
    pm = (layout_n.meta or {}).get("projectmeta") if layout_n.meta else None
    if not pm or not isinstance(pm, dict):
        return (0.0, 0.0)
    w_box = min(290.0, cw0 * 0.38)
    h_box = min(76.0, ch0 * 0.15)
    # Plan occupies [padding, cw0-padding]; title needs more than ``padding`` past the shell on the right and bottom.
    extra_right = max(0.0, w_box + 16.0 - padding)
    extra_bottom = max(0.0, h_box + 16.0 - padding)
    return (extra_right, extra_bottom)


def _planview_annex_reserve(
    layout_n: UnifiedLayout, cw0: float, ch0: float, eff_pad: float
) -> tuple[float, float, float]:
    """Extra canvas beyond title block: (extra_right, extra_bottom, sidebar_w) for vendor-style sheets."""
    er0, eb0 = _title_block_canvas_reserve(layout_n, cw0, ch0, eff_pad)
    meta = layout_n.meta or {}
    pm = meta.get("projectmeta")
    if not isinstance(pm, dict):
        pm = {}
    tabs = meta.get("fixture_vendor_tables")
    has_lfd = isinstance(tabs, dict) and bool((tabs.get("lfd_models") or []))
    style = str(pm.get("drawing_style") or "").strip().lower()
    vendorish = has_lfd or style in ("fixture_vendor", "vendor")
    sidebar_w = 0.0
    if vendorish or str(pm.get("site_panel") or "").strip() in ("1", "true", "yes"):
        sidebar_w = min(132.0, cw0 * 0.28)
    table_h = 0.0
    if has_lfd and isinstance(tabs, dict):
        n = len(tabs["lfd_models"])
        table_h = min(40.0 + n * 10.5, ch0 * 0.55)
    note_h = 0.0
    if str(pm.get("critical_dimensions_note") or "").strip():
        note_h += min(30.0, ch0 * 0.09)
    if str(pm.get("default_materials_note") or "").strip():
        note_h += min(26.0, ch0 * 0.07)
    er = max(er0, sidebar_w + 12.0)
    eb = max(eb0, table_h + note_h + 18.0)
    return er, eb, sidebar_w


def _draw_vendor_annexes(
    dwg: svgwrite.Drawing,
    layout_n: UnifiedLayout,
    *,
    canvas_wh: tuple[float, float],
    font_px: float,
    plan_top: float,
    plan_right: float,
    plan_bottom: float,
    gutter: float,
    sidebar_w: float,
) -> None:
    """LFD schedule table (bottom-left), site sidebar (right of plan), notes — vendor-style fixture layout."""
    cw, ch = canvas_wh
    meta = layout_n.meta or {}
    pm = meta.get("projectmeta")
    if not isinstance(pm, dict):
        pm = {}
    tabs = meta.get("fixture_vendor_tables")
    if not isinstance(tabs, dict):
        tabs = {}
    lfd_rows = tabs.get("lfd_models") or []
    if not isinstance(lfd_rows, list):
        lfd_rows = []
    crit = str(pm.get("critical_dimensions_note") or "").strip()
    mats = str(pm.get("default_materials_note") or "").strip()
    if sidebar_w <= 1.0 and not crit and not mats and not lfd_rows:
        return

    fs_t = max(5.5, min(font_px * 0.34, 8.2))
    fs_b = max(5.0, fs_t * 0.92)
    g = dwg.add(dwg.g(font_family="Arial,sans-serif"))

    # --- Right site panel (areas, frontage, …)
    if sidebar_w > 1.0:
        x0 = plan_right + 6
        wpan = min(sidebar_w - 4.0, cw - x0 - gutter - 4.0)
        y0 = plan_top + 4
        hpan = max(80.0, plan_bottom - plan_top - 8.0)
        if wpan > 36.0:
            g.add(
                dwg.rect(
                    insert=(x0, y0),
                    size=(wpan, hpan),
                    fill="#fafafa",
                    stroke="#94a3b8",
                    stroke_width=0.85,
                    rx=2,
                )
            )
            lines: list[str] = ["SITE / AREAS"]
            if pm.get("store_area_sft") is not None:
                lines.append(f"Store {pm.get('store_area_sft')} SFT")
            if pm.get("proposed_retail_area_sft") is not None:
                lines.append(f"Retail {pm.get('proposed_retail_area_sft')} SFT")
            if pm.get("proposed_stock_room_sft") is not None:
                lines.append(f"Stock {pm.get('proposed_stock_room_sft')} SFT")
            stf = pm.get("proposed_staff_room_sft")
            if stf is not None and str(stf).strip().upper() not in ("NA", "N/A", ""):
                lines.append(f"Staff {stf} SFT")
            elif str(pm.get("proposed_staff_room") or "").strip():
                lines.append(f"Staff {pm.get('proposed_staff_room')}")
            if pm.get("frontage_width"):
                lines.append(f"Frontage {pm.get('frontage_width')}")
            yy = y0 + fs_t + 4
            for i, ln in enumerate(lines):
                g.add(
                    dwg.text(
                        str(ln)[:52],
                        insert=(x0 + 5, yy + i * (fs_b + 2)),
                        text_anchor="start",
                        dominant_baseline="hanging",
                        font_size=f"{fs_t if i == 0 else fs_b}px",
                        fill="#0f172a" if i == 0 else "#334155",
                        font_weight="bold" if i == 0 else "normal",
                    )
                )

    # --- Critical + materials notes (above table)
    y_note = plan_bottom + 8
    if crit:
        g.add(
            dwg.text(
                crit[:220],
                insert=(gutter + 6, y_note),
                text_anchor="start",
                dominant_baseline="hanging",
                font_size=f"{fs_b}px",
                fill="#b91c1c",
                font_weight="bold",
            )
        )
        y_note += fs_b + 6
    if mats:
        g.add(
            dwg.text(
                mats[:220],
                insert=(gutter + 6, y_note),
                text_anchor="start",
                dominant_baseline="hanging",
                font_size=f"{fs_b * 0.95}px",
                fill="#475569",
            )
        )
        y_note += fs_b + 8

    # --- LFD model & niche table
    if not lfd_rows:
        return

    title = str(pm.get("lfd_table_title") or pm.get("vendor_table_title") or "LFD MODEL & NICHE SIZES")[:72]
    x_tbl = gutter + 4
    y_tbl = y_note + 2
    max_w = max(180.0, plan_right - gutter - 24.0)
    row_h = fs_b + 3.5
    n = len(lfd_rows)
    tbl_h = min(row_h * (n + 2) + 18, ch - y_tbl - gutter - 6)

    g.add(
        dwg.rect(
            insert=(x_tbl, y_tbl),
            size=(max_w, tbl_h),
            fill="#ffffff",
            stroke="#64748b",
            stroke_width=0.9,
            rx=2,
        )
    )
    g.add(
        dwg.text(
            title,
            insert=(x_tbl + 6, y_tbl + 8),
            text_anchor="start",
            dominant_baseline="hanging",
            font_size=f"{fs_t + 0.5}px",
            fill="#0f172a",
            font_weight="bold",
        )
    )
    hdr_y = y_tbl + 8 + fs_t + 8
    headers = ("TYPE", "DIAG", "MODEL", "SET WxHxD", "NICHE WxHxD")
    col_x = [x_tbl + 6, x_tbl + 118, x_tbl + 178, x_tbl + 268, x_tbl + 378]

    for i, htxt in enumerate(headers):
        g.add(
            dwg.text(
                htxt,
                insert=(col_x[i], hdr_y),
                text_anchor="start",
                dominant_baseline="hanging",
                font_size=f"{fs_b}px",
                fill="#64748b",
                font_weight="bold",
            )
        )
    g.add(
        dwg.line(
            start=(x_tbl + 4, hdr_y + fs_b + 2),
            end=(x_tbl + max_w - 4, hdr_y + fs_b + 2),
            stroke="#cbd5e1",
            stroke_width=0.75,
        )
    )

    def trunc(s: str, mx: int) -> str:
        s = (s or "").replace("\n", " ")
        return s if len(s) <= mx else s[: mx - 1] + "…"

    ry = hdr_y + fs_b + 8
    for row in lfd_rows[:14]:
        if not isinstance(row, dict):
            continue
        cells = [
            trunc(str(row.get("type", "")), 18),
            trunc(str(row.get("diagonal", "")), 8),
            trunc(str(row.get("model_number", "")), 14),
            trunc(str(row.get("set_dims", "")), 16),
            trunc(str(row.get("niche_dims", "")), 16),
        ]
        for i, txt in enumerate(cells):
            g.add(
                dwg.text(
                    txt,
                    insert=(col_x[i], ry),
                    text_anchor="start",
                    dominant_baseline="hanging",
                    font_size=f"{fs_b * 0.9}px",
                    fill="#0f172a",
                )
            )
        ry += row_h
        if ry > y_tbl + tbl_h - row_h:
            break


def _draw_title_block(
    dwg: svgwrite.Drawing,
    layout_n: UnifiedLayout,
    *,
    canvas_wh: tuple[float, float],
    font_px: float,
) -> None:
    pm = (layout_n.meta or {}).get("projectmeta")
    if not pm or not isinstance(pm, dict):
        return
    cw, ch = canvas_wh
    w_box = min(290.0, cw * 0.38)
    h_box = min(76.0, ch * 0.15)
    x0 = cw - w_box - 12
    y0 = ch - h_box - 8
    fs_t = max(7.0, min(font_px * 0.36, 10.5))
    g = dwg.add(dwg.g(font_family="Arial,sans-serif"))
    g.add(
        dwg.rect(
            insert=(x0, y0),
            size=(w_box, h_box),
            fill="#f1f5f9",
            stroke="#475569",
            stroke_width=1,
            rx=2,
        )
    )
    title = str(pm.get("client") or pm.get("project_name") or "")[:72]
    sub = str(pm.get("drawing_title") or "Layout")[:56]
    scale = str(pm.get("scale") or "")
    dn = str(pm.get("drawing_number") or "")
    line2 = f"{sub}"
    if scale:
        line2 += f" | Scale {scale}"
    parts: list[str] = []
    if pm.get("store_area_sft") is not None:
        parts.append(f"STORE {pm.get('store_area_sft')} SFT")
    if pm.get("proposed_retail_area_sft") is not None:
        parts.append(f"RETAIL {pm.get('proposed_retail_area_sft')} SFT")
    if pm.get("proposed_stock_room_sft") is not None:
        parts.append(f"STOCK {pm.get('proposed_stock_room_sft')} SFT")
    line3 = " | ".join(parts)
    cx = x0 + w_box / 2
    yy = y0 + fs_t + 5
    if title.strip():
        g.add(
            dwg.text(
                title,
                insert=(cx, yy),
                text_anchor="middle",
                dominant_baseline="hanging",
                font_size=f"{fs_t + 0.5}px",
                fill="#0f172a",
                font_weight="bold",
            )
        )
        yy += fs_t + 6
    if line2.strip():
        g.add(
            dwg.text(
                line2,
                insert=(cx, yy),
                text_anchor="middle",
                dominant_baseline="hanging",
                font_size=f"{fs_t * 0.88}px",
                fill="#475569",
            )
        )
        yy += fs_t + 2
    if line3.strip():
        g.add(
            dwg.text(
                line3,
                insert=(cx, yy),
                text_anchor="middle",
                dominant_baseline="hanging",
                font_size=f"{fs_t * 0.82}px",
                fill="#b91c1c",
                font_weight="bold",
            )
        )
        yy += fs_t
    if dn.strip():
        g.add(
            dwg.text(
                dn + " · " + str(layout_n.units),
                insert=(cx, yy),
                text_anchor="middle",
                dominant_baseline="hanging",
                font_size=f"{fs_t * 0.78}px",
                fill="#64748b",
            )
        )


def _draw_vector_plan(
    dwg: svgwrite.Drawing,
    layout_n: UnifiedLayout,
    tx,
    *,
    canvas_wh: tuple[float, float],
    wall_px: float,
    font_px: float,
    with_background: bool,
    caption_at_top: bool = False,
    scale_mm_to_px: float = 1.0,
    frame_gutter: float = 0.0,
    annex: dict[str, float] | None = None,
) -> None:
    """Shared 2D plan styling: tinted rooms, double-stroke walls, openings, fixtures, labels."""
    cw, ch = canvas_wh
    if with_background:
        g = max(0.0, float(frame_gutter))
        if g > 0.0:
            dwg.add(dwg.rect(insert=(0, 0), size=(cw, ch), fill="#ffffff", stroke="none"))
            inner_w, inner_h = max(1.0, cw - 2 * g), max(1.0, ch - 2 * g)
            dwg.add(
                dwg.rect(
                    insert=(g, g),
                    size=(inner_w, inner_h),
                    fill="#f1f5f9",
                    stroke="#cbd5e1",
                    stroke_width=1,
                    rx=6,
                )
            )
        else:
            dwg.add(
                dwg.rect(
                    insert=(0, 0),
                    size=(cw, ch),
                    fill="#f1f5f9",
                    stroke="#cbd5e1",
                    stroke_width=1,
                    rx=6,
                )
            )

    pat = dwg.pattern(id="pf_store_hatch_diag", size=(7, 7), patternUnits="userSpaceOnUse")
    pat.add(dwg.path(d="M0,7 L7,0", stroke="#94a3b8", stroke_width=0.45))
    dwg.defs.add(pat)

    rooms_g = dwg.add(dwg.g())
    for i, room in enumerate(layout_n.rooms):
        if len(room.polygon) < 3:
            continue
        pts = [tx(float(p[0]), float(p[1])) for p in room.polygon]
        fill_c, use_hatch = _room_visual(room.name, i)
        rooms_g.add(
            dwg.polygon(
                points=pts,
                fill=fill_c,
                fill_opacity=0.72,
                stroke="#334155",
                stroke_width=max(0.8, wall_px * 0.35),
                stroke_linejoin="round",
            )
        )
        if use_hatch:
            rooms_g.add(
                dwg.polygon(
                    points=pts,
                    fill="url(#pf_store_hatch_diag)",
                    fill_opacity=0.42,
                    stroke="none",
                )
            )

    sw = max(wall_px * 1.85, 2.0)
    shadow = dwg.add(
        dwg.g(
            stroke="#94a3b8",
            fill="none",
            stroke_width=sw,
            stroke_linecap="round",
            stroke_linejoin="round",
            opacity=0.85,
        )
    )
    walls_g = dwg.add(
        dwg.g(
            stroke="#0f172a",
            fill="none",
            stroke_width=wall_px,
            stroke_linecap="round",
            stroke_linejoin="round",
        )
    )
    for wall in layout_n.walls:
        x1, y1 = tx(wall.x1, wall.y1)
        x2, y2 = tx(wall.x2, wall.y2)
        shadow.add(dwg.line(start=(x1, y1), end=(x2, y2)))
        walls_g.add(dwg.line(start=(x1, y1), end=(x2, y2)))

    op_g = dwg.add(
        dwg.g(
            stroke="#c2410c",
            fill="none",
            stroke_width=max(1.2, wall_px * 0.55),
            stroke_dasharray="10,6",
            stroke_linecap="round",
            opacity=0.95,
        )
    )
    for o in layout_n.openings:
        x1, y1 = tx(o.x1, o.y1)
        x2, y2 = tx(o.x2, o.y2)
        op_g.add(dwg.line(start=(x1, y1), end=(x2, y2)))

    op_lbl = dwg.add(dwg.g(font_family="Arial,sans-serif", font_weight="600"))
    fs_op = max(6.0, min(font_px * 0.4, 11.0))
    for o in layout_n.openings:
        if not o.label:
            continue
        mx, my = (o.x1 + o.x2) / 2.0, (o.y1 + o.y2) / 2.0
        sx, sy = tx(mx, my)
        op_lbl.add(
            dwg.text(
                str(o.label),
                insert=(sx, sy),
                text_anchor="middle",
                dominant_baseline="middle",
                font_size=f"{fs_op}px",
                fill="#9a3412",
            )
        )

    _draw_fixtures_rich(dwg, layout_n, tx, wall_px=wall_px, font_px=font_px)

    _draw_svg_dimensions(
        dwg,
        layout_n,
        tx,
        scale_mm_to_px=scale_mm_to_px,
        wall_px=wall_px,
        font_px=font_px,
        with_background=with_background,
    )

    lbl = dwg.add(dwg.g(font_family="'Segoe UI',system-ui,sans-serif", font_weight="600"))
    fs = max(11.0, min(font_px, 28.0))
    lbl_stroke = max(2.5, fs * 0.18) if with_background else max(4.5, fs * 0.28)
    lbl_fill = "#0f172a" if with_background else "#f8fafc"
    lbl_outline = "#ffffff" if with_background else "#0f172a"
    for room in layout_n.rooms:
        if len(room.polygon) < 3:
            continue
        poly = SPolygon([(float(p[0]), float(p[1])) for p in room.polygon])
        if not poly.is_valid:
            continue
        c = poly.centroid
        cx, cy = tx(c.x, c.y)
        name = room.name or "Room"
        lbl.add(
            dwg.text(
                name,
                insert=(cx, cy),
                text_anchor="middle",
                dominant_baseline="middle",
                fill=lbl_fill,
                stroke=lbl_outline,
                stroke_width=lbl_stroke,
                font_size=f"{fs}px",
                style="paint-order: stroke fill;",
            )
        )
        if room.area is not None:
            try:
                au = float(room.area)
                if au <= 0:
                    raise ValueError
                lbl.add(
                    dwg.text(
                        f"{int(au)} SFT" if au >= 10 else f"{au:g} SFT",
                        insert=(cx, cy + fs * 0.95),
                        text_anchor="middle",
                        dominant_baseline="middle",
                        fill=lbl_fill,
                        stroke=lbl_outline,
                        stroke_width=lbl_stroke * 0.85,
                        font_size=f"{fs * 0.62}px",
                        opacity=0.88,
                        style="paint-order: stroke fill;",
                    )
                )
            except (TypeError, ValueError):
                pass

    if annex:
        _draw_vendor_annexes(
            dwg,
            layout_n,
            canvas_wh=canvas_wh,
            font_px=font_px,
            plan_top=float(annex["plan_top"]),
            plan_right=float(annex["plan_right"]),
            plan_bottom=float(annex["plan_bottom"]),
            gutter=float(annex["gutter"]),
            sidebar_w=float(annex.get("sidebar_w") or 0.0),
        )

    _draw_title_block(dwg, layout_n, canvas_wh=canvas_wh, font_px=font_px)

    hint = f"units: {layout_n.units}"
    if with_background:
        hint += " · 2D vector plan"
    ty = 22 if caption_at_top else ch - 12
    cap_fill = "#475569" if with_background else "#f8fafc"
    cap_stroke = "none" if with_background else "#0f172a"
    cap_sw = 0 if with_background else max(2.0, fs * 0.12)
    caption = dwg.add(
        dwg.g(
            fill=cap_fill,
            stroke=cap_stroke,
            stroke_width=cap_sw,
            font_family="system-ui,sans-serif",
            font_size=f"{max(10, fs * 0.55)}px",
            font_weight="500",
        )
    )
    cap_kw: dict = {"insert": (12, ty), "dominant_baseline": "auto"}
    if not with_background:
        cap_kw["style"] = "paint-order: stroke fill;"
    caption.add(dwg.text(hint, **cap_kw))


def _bounds(layout: UnifiedLayout) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for w in layout.walls:
        xs.extend([w.x1, w.x2])
        ys.extend([w.y1, w.y2])
    for r in layout.rooms:
        for pt in r.polygon:
            if len(pt) >= 2:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
    if not xs or not ys:
        return 0.0, 0.0, 1.0, 1.0
    return min(xs), min(ys), max(xs), max(ys)


def _bounds_including_sheet_dimensions(layout: UnifiedLayout) -> tuple[float, float, float, float]:
    """Extend axes bounds so Excel dimension strings drawn outside the shell are not clipped."""
    minx, miny, maxx, maxy = _bounds(layout)
    dims = layout.meta.get("svg_dimensions") if layout.meta else None
    if not dims or not isinstance(dims, list):
        return minx, miny, maxx, maxy
    off = float(layout.meta.get("svg_dimension_offset_mm") or 380.0)
    xs = [minx, maxx]
    ys = [miny, maxy]
    for d in dims:
        if not isinstance(d, dict):
            continue
        try:
            x1, y1 = float(d["x1"]), float(d["y1"])
            x2, y2 = float(d["x2"]), float(d["y2"])
        except (KeyError, TypeError, ValueError):
            continue
        nx = float(d.get("nx") or 0.0)
        ny = float(d.get("ny") or 0.0)
        ln = math.hypot(nx, ny)
        if ln > 1e-9:
            nx, ny = nx / ln, ny / ln
        for x, y in ((x1, y1), (x2, y2)):
            xs.extend([x, x + nx * off, x + nx * off * 1.25])
            ys.extend([y, y + ny * off, y + ny * off * 1.25])
    return min(xs), min(ys), max(xs), max(ys)


def normalize_to_origin(layout: UnifiedLayout) -> tuple[UnifiedLayout, tuple[float, float]]:
    """Translate layout so min corner is near origin; returns (new_layout, offset)."""
    minx, miny, _, _ = _bounds_including_sheet_dimensions(layout)
    ox, oy = minx, miny

    walls_fixed = [
        WallSegment(x1=w.x1 - ox, y1=w.y1 - oy, x2=w.x2 - ox, y2=w.y2 - oy)
        for w in layout.walls
    ]
    rooms = []

    for r in layout.rooms:
        poly = [[float(p[0]) - ox, float(p[1]) - oy] for p in r.polygon if len(p) >= 2]
        rooms.append(Room(name=r.name, area=r.area, polygon=poly))

    fixtures = [
        Fixture(
            kind=f.kind,
            x=f.x - ox,
            y=f.y - oy,
            label=f.label,
            width_mm=f.width_mm,
            depth_mm=f.depth_mm,
            height_mm=f.height_mm,
            rotation_deg=f.rotation_deg,
        )
        for f in layout.fixtures
    ]
    openings = [
        Opening(
            kind=o.kind,
            x1=o.x1 - ox,
            y1=o.y1 - oy,
            x2=o.x2 - ox,
            y2=o.y2 - oy,
            label=o.label,
        )
        for o in layout.openings
    ]

    meta_adj = _translate_svg_dimensions_meta(dict(layout.meta), ox, oy)

    ul = UnifiedLayout(
        units=layout.units,
        walls=walls_fixed,
        rooms=rooms,
        fixtures=fixtures,
        openings=openings,
        meta=meta_adj,
    )
    return ul, (ox, oy)


def light_snap_walls(layout: UnifiedLayout, tol: float = 5.0) -> UnifiedLayout:
    """Merge nearly-colinear segments sharing endpoints (MVP grid snap)."""
    if not layout.walls:
        return layout
    lines = [LineString([(w.x1, w.y1), (w.x2, w.y2)]) for w in layout.walls]
    merged = unary_union(lines)
    if isinstance(merged, MultiLineString):
        geoms = list(merged.geoms)
    elif isinstance(merged, LineString):
        geoms = [merged]
    else:
        geoms = getattr(merged, "geoms", [merged])
    walls_out: list[WallSegment] = []
    for g in geoms:
        c = getattr(g, "coords", None)
        if c is None:
            continue
        pts = list(c)
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            if math.hypot(x2 - x1, y2 - y1) < tol:
                continue
            walls_out.append(WallSegment(x1=float(x1), y1=float(y1), x2=float(x2), y2=float(y2)))
    if not walls_out:
        return layout
    return UnifiedLayout(
        units=layout.units,
        walls=walls_out,
        rooms=layout.rooms,
        fixtures=layout.fixtures,
        openings=layout.openings,
        meta={**layout.meta, "wall_snap_tol": tol},
    )


def layout_to_svg(
    layout: UnifiedLayout,
    *,
    padding: float = 52.0,
    scale: float | None = None,
    underlay_png_path: Path | str | None = None,
    reference_plan_path: Path | str | None = None,
) -> str:
    """
    Render SVG. When ``underlay_png_path`` points to a readable image, embed it as a bottom layer
    and draw vectors in native pixel coordinates (Phase 3). Otherwise use scaled normalized layout.

    ``reference_plan_path`` (optional): raster floor plan stretched behind vector output for sheet
    layouts (set ``PLANFORGE_REFERENCE_PLAN_IMAGE``). Does not replace geometry — vectors + Excel
    dimensions stay authoritative.
    """
    if underlay_png_path:
        path = Path(underlay_png_path)
        if path.is_file():
            from app.services.image_processing_utils import png_base64_data_uri

            try:
                data_uri, iw, ih = png_base64_data_uri(path)
            except (ValueError, OSError):
                pass
            else:
                return _layout_to_svg_with_underlay(layout, data_uri, iw, ih)

    return _layout_to_svg_scaled(
        layout,
        padding=padding,
        scale=scale,
        reference_plan_path=reference_plan_path,
    )


def _layout_to_svg_with_underlay(layout: UnifiedLayout, data_uri: str, iw: int, ih: int) -> str:
    """Pixel-aligned SVG: scan underlay + architectural overlays (same coord space as image)."""
    dwg = svgwrite.Drawing(size=(f"{iw}px", f"{ih}px"), viewBox=f"0 0 {iw} {ih}")
    dwg.add(dwg.image(href=data_uri, insert=(0, 0), size=(iw, ih)))

    def tx(x: float, y: float) -> tuple[float, float]:
        return float(x), float(y)

    m = max(iw, ih, 1)
    wall_px = max(1.8, min(5.5, m / 320.0))
    font_px = max(11.0, min(22.0, m / 55.0))
    _draw_vector_plan(
        dwg,
        layout,
        tx,
        canvas_wh=(float(iw), float(ih)),
        wall_px=wall_px,
        font_px=font_px,
        with_background=False,
        caption_at_top=True,
        scale_mm_to_px=1.0,
    )
    return dwg.tostring()


def _layout_to_svg_scaled(
    layout: UnifiedLayout,
    *,
    padding: float,
    scale: float | None,
    reference_plan_path: Path | str | None = None,
) -> str:
    layout_n, _ = normalize_to_origin(layout)
    minx, miny, maxx, maxy = _bounds(layout_n)
    w = max(maxx - minx, 1.0)
    h = max(maxy - miny, 1.0)

    if scale is None:
        target = 960.0
        scale = target / max(w, h)

    gutter = _LAYOUT_SVG_OUTER_GUTTER_PX
    eff_pad = max(float(padding), _MIN_LAYOUT_BORDER_PX)
    cw0 = w * scale + 2 * eff_pad
    ch0 = h * scale + 2 * eff_pad
    er, eb, sidebar_w = _planview_annex_reserve(layout_n, cw0, ch0, eff_pad)
    cw, ch = cw0 + er + 2 * gutter, ch0 + eb + 2 * gutter

    def tx(x: float, y: float) -> tuple[float, float]:
        return (
            (x - minx) * scale + eff_pad + gutter,
            (maxy - y) * scale + eff_pad + gutter,
        )

    wall_px = max(2.0, min(7.0, scale * 2.85))
    font_px = max(12.0, min(24.0, scale * 5.8))

    plan_top = eff_pad + gutter
    plan_right = cw0 - eff_pad + gutter
    plan_bottom = eff_pad + gutter + h * scale
    annex: dict[str, float] = {
        "plan_top": plan_top,
        "plan_right": plan_right,
        "plan_bottom": plan_bottom,
        "gutter": float(gutter),
        "sidebar_w": float(sidebar_w),
    }

    dwg = svgwrite.Drawing(size=(f"{cw}px", f"{ch}px"))

    ref_p = Path(reference_plan_path) if reference_plan_path else None
    if ref_p and ref_p.is_file():
        from app.services.image_processing_utils import png_base64_data_uri

        try:
            data_uri, _iw, _ih = png_base64_data_uri(ref_p)
        except (ValueError, OSError):
            pass
        else:
            dwg.add(
                dwg.image(
                    href=data_uri,
                    insert=(0, 0),
                    size=(cw, ch),
                    opacity=0.42,
                    preserveAspectRatio="none",
                )
            )

    _draw_vector_plan(
        dwg,
        layout_n,
        tx,
        canvas_wh=(cw, ch),
        wall_px=wall_px,
        font_px=font_px,
        with_background=True,
        caption_at_top=False,
        scale_mm_to_px=scale,
        frame_gutter=gutter,
        annex=annex,
    )
    return dwg.tostring()
