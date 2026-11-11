"""
DXF exporter — civil/architectural-grade output from UnifiedLayout.

Layers (IS/ISO standard colour codes):
  WALLS        colour 5  (blue)   — perimeter + partition walls
  ROOMS        colour 8  (grey)   — room boundary hatches
  FIXTURES     colour 1  (red)    — fixture outlines
  OPENINGS     colour 3  (green)  — doors / windows
  DIMENSIONS   colour 2  (yellow) — all dimension entities
  ANNOTATIONS  colour 7  (white)  — room labels, fixture labels
  TITLEBLOCK   colour 6  (magenta)— title block border + text
  SCALEBAR     colour 4  (cyan)   — scale bar + north arrow

Dimension style: PLANFORGE_DIM
  dimtxt  = 80 mm   text height
  dimasz  = 60 mm   arrow size
  dimexe  = 80 mm   extension line extension beyond dim line
  dimexo  = 40 mm   extension line offset from origin
  dimscale = 1.0
  arrows  = closed_filled (ACAD standard)

All coordinates are in the layout's native units (mm unless stated otherwise).
"""

from __future__ import annotations

import math
import logging
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Vec2
from ezdxf.tools.standards import setup_linetypes as _ezdxf_setup_linetypes

from app.models.layout_schema import UnifiedLayout
from app.services.sheet_parser import _LABEL_MAX_CHARS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DIM_OFFSET = 200.0        # distance from geometry edge to first dim line (mm)
_DIM_CHAIN_GAP = 250.0     # gap between stacked dimension lines (mm)
_TEXT_HEIGHT_ROOM = 120.0  # room label text height (mm)
_TEXT_HEIGHT_FIX = 80.0    # fixture label text height (mm)
_TEXT_HEIGHT_DIM = 80.0    # dimension text height (mm)
_ARROW_SIZE = 60.0
_EXT_OVERSHOOT = 80.0      # extension line beyond dim line
_EXT_OFFSET = 40.0         # extension line gap from geometry point

_LAYER_WALLS = "WALLS"
_LAYER_ROOMS = "ROOMS"
_LAYER_FIXTURES = "FIXTURES"
_LAYER_OPENINGS = "OPENINGS"
_LAYER_DIMS = "DIMENSIONS"
_LAYER_ANNOT = "ANNOTATIONS"
_LAYER_TITLE = "TITLEBLOCK"
_LAYER_SCALE = "SCALEBAR"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_layers(doc: ezdxf.document.Drawing) -> None:
    pairs = [
        (_LAYER_WALLS,    5),
        (_LAYER_ROOMS,    8),
        (_LAYER_FIXTURES, 1),
        (_LAYER_OPENINGS, 3),
        (_LAYER_DIMS,     2),
        (_LAYER_ANNOT,    7),
        (_LAYER_TITLE,    6),
        (_LAYER_SCALE,    4),
    ]
    for name, colour in pairs:
        if name not in doc.layers:
            doc.layers.add(name, dxfattribs={"color": colour})


def _setup_text_style(doc: ezdxf.document.Drawing) -> str:
    style_name = "PLANFORGE_LABEL"
    if style_name not in doc.styles:
        doc.styles.new(style_name, dxfattribs={"font": "Arial.ttf", "bigfont": ""})
    return style_name


def _setup_dimstyle(doc: ezdxf.document.Drawing) -> str:
    style_name = "PLANFORGE_DIM"
    if style_name not in doc.dimstyles:
        doc.dimstyles.new(
            style_name,
            dxfattribs={
                "dimtxt":   _TEXT_HEIGHT_DIM,
                "dimasz":   _ARROW_SIZE,
                "dimexe":   _EXT_OVERSHOOT,
                "dimexo":   _EXT_OFFSET,
                "dimscale": 1.0,
                "dimblk":   "",  # "" = CLOSED_FILLED arrow (ezdxf default)
                "dimclrd":  2,   # dim line colour = yellow
                "dimclre":  2,   # ext line colour = yellow
                "dimclrt":  7,   # text colour = white
                "dimtxsty": _text_style_name,
            },
        )
    return style_name


_text_style_name: str = "PLANFORGE_LABEL"  # set once during export_layout_to_dxf


def _add_text(msp: Any, text: str, insert: tuple[float, float], height: float, layer: str,
              halign: int = 1, valign: int = 1, rotation: float = 0.0) -> None:
    """
    Add a centred label. Uses MTEXT when the text contains newlines so that
    multi-line room/fixture labels render correctly in all viewers. Falls back
    to TEXT for single-line strings for maximum compatibility.
    """
    if "\n" in text:
        # MTEXT: supports real newlines, paragraph flow, and font embedding
        try:
            mtext = msp.add_mtext(
                text.replace("\n", "\\P"),  # ezdxf MTEXT paragraph break
                dxfattribs={
                    "insert": insert,
                    "char_height": height,
                    "layer": layer,
                    "style": _text_style_name,
                    "rotation": rotation,
                    "attachment_point": 5,  # 5 = MIDDLE_CENTER
                },
            )
            mtext.set_location(insert, rotation=rotation, attachment_point=5)
            return
        except Exception:
            # Fallback: join lines with space and use single TEXT
            text = "  ".join(text.split("\n"))

    attribs: dict[str, Any] = {
        "insert": insert,
        "height": height,
        "layer": layer,
        "style": _text_style_name,
        "rotation": rotation,
    }
    try:
        msp.add_text(text, dxfattribs=attribs).set_placement(
            insert, align=TextEntityAlignment.MIDDLE_CENTER
        )
    except Exception:
        msp.add_text(text, dxfattribs=attribs)


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
    for f in layout.fixtures:
        hw = (f.width_mm or 0) / 2
        hd = (f.depth_mm or 0) / 2
        xs.extend([f.x - hw, f.x + hw])
        ys.extend([f.y - hd, f.y + hd])
    if not xs or not ys:
        return 0.0, 0.0, 1000.0, 1000.0
    return min(xs), min(ys), max(xs), max(ys)


def _fixture_corners(f: Any) -> list[tuple[float, float]] | None:
    w, d = f.width_mm, f.depth_mm
    if not w or not d or w <= 0 or d <= 0:
        return None
    rad = math.radians(f.rotation_deg or 0.0)
    c, s = math.cos(rad), math.sin(rad)
    cx, cy = f.x, f.y
    local = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
    return [(cx + c * lx - s * ly, cy + s * lx + c * ly) for lx, ly in local]


# ---------------------------------------------------------------------------
# Section A — Walls
# ---------------------------------------------------------------------------

def _draw_walls(msp: Any, layout: UnifiedLayout) -> None:
    for w in layout.walls:
        msp.add_line(
            start=(w.x1, w.y1),
            end=(w.x2, w.y2),
            dxfattribs={"layer": _LAYER_WALLS, "lineweight": 50},
        )


# ---------------------------------------------------------------------------
# Section B — Rooms (boundary polyline + hatch + label)
# ---------------------------------------------------------------------------

def _draw_rooms(msp: Any, layout: UnifiedLayout) -> None:
    for room in layout.rooms:
        poly = room.polygon
        if len(poly) < 3:
            continue
        pts = [Vec2(float(p[0]), float(p[1])) for p in poly]

        # Boundary polyline
        msp.add_lwpolyline(
            pts,
            close=True,
            dxfattribs={"layer": _LAYER_ROOMS, "lineweight": 25},
        )

        # Solid hatch (very light — pattern ANSI31 at large scale so it reads as tint)
        try:
            hatch = msp.add_hatch(color=8, dxfattribs={"layer": _LAYER_ROOMS})
            hatch.set_pattern_fill("ANSI31", scale=500.0, angle=45.0)
            hatch.paths.add_polyline_path(
                [(float(p[0]), float(p[1])) for p in poly], is_closed=True
            )
        except Exception as exc:
            logger.debug("Hatch skipped for room %r: %s", room.name, exc)

        # Centroid label
        cx = sum(float(p[0]) for p in poly) / len(poly)
        cy = sum(float(p[1]) for p in poly) / len(poly)
        label = room.name or "Room"
        if room.area:
            try:
                area_unit = (layout.units or "mm").upper()
                label += f"\n{int(room.area)} {area_unit}²"
            except (TypeError, ValueError):
                pass
        _add_text(msp, label, (cx, cy), _TEXT_HEIGHT_ROOM, _LAYER_ANNOT)


# ---------------------------------------------------------------------------
# Section C — Fixtures (outline + label + per-fixture dimensions)
# ---------------------------------------------------------------------------

def _draw_fixtures(msp: Any, layout: UnifiedLayout, dim_style: str) -> None:
    for f in layout.fixtures:
        corners = _fixture_corners(f)
        label = (f.label or f.kind or "")[:_LABEL_MAX_CHARS]
        w_mm = f.width_mm or 0.0
        d_mm = f.depth_mm or 0.0

        if corners and len(corners) == 4:
            pts = [Vec2(x, y) for x, y in corners]
            msp.add_lwpolyline(
                pts,
                close=True,
                dxfattribs={"layer": _LAYER_FIXTURES, "lineweight": 35},
            )

            # Fixture label at centre
            cx = sum(x for x, _ in corners) / 4
            cy = sum(y for _, y in corners) / 4
            dim_label = f"{label}\n{w_mm:.0f}×{d_mm:.0f}" if w_mm and d_mm else label
            _add_text(msp, dim_label, (cx, cy), _TEXT_HEIGHT_FIX, _LAYER_ANNOT)

            # Only add dimensions when fixture is axis-aligned (rotation ≈ 0 or 90)
            rot = (f.rotation_deg or 0.0) % 360
            axis_aligned = abs(rot) < 1.0 or abs(rot - 90) < 1.0 or abs(rot - 180) < 1.0 or abs(rot - 270) < 1.0

            if axis_aligned and w_mm > 0 and d_mm > 0:
                # Use the unrotated bounding box for clean dimension placement
                xs = [x for x, _ in corners]
                ys = [y for _, y in corners]
                x0, x1 = min(xs), max(xs)
                y0, y1 = min(ys), max(ys)

                # Horizontal dimension (width) — below fixture
                try:
                    msp.add_linear_dim(
                        base=(x0, y0 - _DIM_OFFSET),
                        p1=(x0, y0),
                        p2=(x1, y0),
                        dimstyle=dim_style,
                        dxfattribs={"layer": _LAYER_DIMS},
                    ).render()
                except Exception as exc:
                    logger.debug("H-dim skipped for fixture %r: %s", label, exc)

                # Vertical dimension (depth) — left of fixture
                try:
                    msp.add_linear_dim(
                        base=(x0 - _DIM_OFFSET, y0),
                        p1=(x0, y0),
                        p2=(x0, y1),
                        angle=90,
                        dimstyle=dim_style,
                        dxfattribs={"layer": _LAYER_DIMS},
                    ).render()
                except Exception as exc:
                    logger.debug("V-dim skipped for fixture %r: %s", label, exc)

        else:
            # Fallback: circle for point fixtures
            msp.add_circle(
                center=(f.x, f.y),
                radius=max(50.0, min(w_mm, d_mm) / 2 if w_mm and d_mm else 100.0),
                dxfattribs={"layer": _LAYER_FIXTURES},
            )
            if label:
                _add_text(msp, label, (f.x, f.y), _TEXT_HEIGHT_FIX, _LAYER_ANNOT)


# ---------------------------------------------------------------------------
# Section D — Openings (dashed line + label + width dimension)
# ---------------------------------------------------------------------------

def _draw_openings(msp: Any, layout: UnifiedLayout, dim_style: str) -> None:
    for o in layout.openings:
        msp.add_line(
            start=(o.x1, o.y1),
            end=(o.x2, o.y2),
            dxfattribs={
                "layer": _LAYER_OPENINGS,
                "linetype": "DASHED",
                "lineweight": 18,
            },
        )
        # Label at midpoint
        mx, my = (o.x1 + o.x2) / 2, (o.y1 + o.y2) / 2
        lbl = o.label or o.kind or "Opening"
        _add_text(msp, lbl, (mx, my + 80), _TEXT_HEIGHT_FIX * 0.8, _LAYER_ANNOT)

        # Width dimension (aligned — works for diagonal openings too)
        length = math.hypot(o.x2 - o.x1, o.y2 - o.y1)
        if length > 10:
            try:
                # Perpendicular offset direction
                dx, dy = (o.x2 - o.x1) / length, (o.y2 - o.y1) / length
                nx, ny = -dy, dx  # normal
                bx = mx + nx * _DIM_OFFSET * 0.6
                by = my + ny * _DIM_OFFSET * 0.6
                msp.add_aligned_dim(
                    p1=(o.x1, o.y1),
                    p2=(o.x2, o.y2),
                    dist=_DIM_OFFSET * 0.6,
                    dimstyle=dim_style,
                    dxfattribs={"layer": _LAYER_DIMS},
                ).render()
            except Exception as exc:
                logger.debug("Opening dim skipped: %s", exc)


# ---------------------------------------------------------------------------
# Section E — Overall store dimensions (perimeter chain)
# ---------------------------------------------------------------------------

def _draw_overall_dimensions(
    msp: Any,
    layout: UnifiedLayout,
    dim_style: str,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
) -> None:
    """
    Four overall dimensions: top, bottom, left, right of the bounding box.
    Stacked at _DIM_OFFSET from the geometry edge.
    """
    gap = _DIM_OFFSET

    # Bottom overall — horizontal
    try:
        msp.add_linear_dim(
            base=(minx, miny - gap),
            p1=(minx, miny),
            p2=(maxx, miny),
            dimstyle=dim_style,
            dxfattribs={"layer": _LAYER_DIMS},
        ).render()
    except Exception as exc:
        logger.debug("Overall bottom dim failed: %s", exc)

    # Top overall — horizontal
    try:
        msp.add_linear_dim(
            base=(minx, maxy + gap),
            p1=(minx, maxy),
            p2=(maxx, maxy),
            dimstyle=dim_style,
            dxfattribs={"layer": _LAYER_DIMS},
        ).render()
    except Exception as exc:
        logger.debug("Overall top dim failed: %s", exc)

    # Left overall — vertical
    try:
        msp.add_linear_dim(
            base=(minx - gap, miny),
            p1=(minx, miny),
            p2=(minx, maxy),
            angle=90,
            dimstyle=dim_style,
            dxfattribs={"layer": _LAYER_DIMS},
        ).render()
    except Exception as exc:
        logger.debug("Overall left dim failed: %s", exc)

    # Right overall — vertical
    try:
        msp.add_linear_dim(
            base=(maxx + gap, miny),
            p1=(maxx, miny),
            p2=(maxx, maxy),
            angle=90,
            dimstyle=dim_style,
            dxfattribs={"layer": _LAYER_DIMS},
        ).render()
    except Exception as exc:
        logger.debug("Overall right dim failed: %s", exc)


# ---------------------------------------------------------------------------
# Section F — Room-to-room partition dimensions (chain along X and Y)
# ---------------------------------------------------------------------------

def _draw_partition_dimensions(
    msp: Any,
    layout: UnifiedLayout,
    dim_style: str,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
) -> None:
    """
    Collect unique X and Y coordinates from room polygon vertices, then draw a
    chained dimension string along the bottom and left edges. Uses rooms only
    (not wall endpoints, which duplicate room corners and add noise).
    """
    x_coords: set[float] = set()
    y_coords: set[float] = set()

    for r in layout.rooms:
        for pt in r.polygon:
            if len(pt) >= 2:
                x_coords.add(round(float(pt[0]), 1))
                y_coords.add(round(float(pt[1]), 1))

    xs = sorted(x_coords)
    ys = sorted(y_coords)

    chain_y = miny - _DIM_OFFSET * 2.2   # second row below overall
    chain_x = minx - _DIM_OFFSET * 2.2   # second column left of overall

    # Horizontal chain (bottom)
    for i in range(len(xs) - 1):
        x1, x2 = xs[i], xs[i + 1]
        if x2 - x1 < 50:
            continue
        try:
            msp.add_linear_dim(
                base=(x1, chain_y),
                p1=(x1, miny),
                p2=(x2, miny),
                dimstyle=dim_style,
                dxfattribs={"layer": _LAYER_DIMS},
            ).render()
        except Exception as exc:
            logger.debug("Partition H-dim failed: %s", exc)

    # Vertical chain (left)
    for i in range(len(ys) - 1):
        y1, y2 = ys[i], ys[i + 1]
        if y2 - y1 < 50:
            continue
        try:
            msp.add_linear_dim(
                base=(chain_x, y1),
                p1=(minx, y1),
                p2=(minx, y2),
                angle=90,
                dimstyle=dim_style,
                dxfattribs={"layer": _LAYER_DIMS},
            ).render()
        except Exception as exc:
            logger.debug("Partition V-dim failed: %s", exc)


# ---------------------------------------------------------------------------
# Section G — Elevation / section callout markers
# ---------------------------------------------------------------------------

def _draw_elevation_markers(
    msp: Any,
    layout: UnifiedLayout,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
) -> None:
    """
    Place standard section-cut symbols (circle + arrow) at the four cardinal
    edges of the plan. These are reference markers — the actual elevation views
    would be drawn in separate paper-space viewports or additional model-space
    regions. Each marker is labelled A–D.
    """
    cx = (minx + maxx) / 2
    cy = (miny + maxy) / 2
    r = 120.0  # circle radius for section symbol

    markers = [
        ("A", cx, maxy + _DIM_OFFSET * 3.5, 0.0),    # North elevation
        ("B", maxx + _DIM_OFFSET * 3.5, cy, 270.0),   # East elevation
        ("C", cx, miny - _DIM_OFFSET * 3.5, 180.0),   # South elevation
        ("D", minx - _DIM_OFFSET * 3.5, cy, 90.0),    # West elevation
    ]

    for label, mx, my, angle_deg in markers:
        # Circle
        msp.add_circle(
            center=(mx, my),
            radius=r,
            dxfattribs={"layer": _LAYER_ANNOT},
        )
        # Arrow line from circle edge toward plan
        rad = math.radians(angle_deg)
        ax = mx + math.cos(rad) * r
        ay = my + math.sin(rad) * r
        bx = mx + math.cos(rad) * (r + 200)
        by = my + math.sin(rad) * (r + 200)
        msp.add_line(
            start=(ax, ay),
            end=(bx, by),
            dxfattribs={"layer": _LAYER_ANNOT},
        )
        # Label inside circle
        _add_text(msp, label, (mx, my), r * 0.9, _LAYER_ANNOT)

    # Horizontal section cut line across plan (Section A-A)
    cut_y = cy
    msp.add_line(
        start=(minx - _DIM_OFFSET * 0.5, cut_y),
        end=(maxx + _DIM_OFFSET * 0.5, cut_y),
        dxfattribs={"layer": _LAYER_ANNOT, "linetype": "PHANTOM"},
    )
    _add_text(msp, "SECTION A-A", (cx, cut_y + 80), _TEXT_HEIGHT_FIX * 0.75, _LAYER_ANNOT)


# ---------------------------------------------------------------------------
# Section H — BOM / fixture schedule callout table
# ---------------------------------------------------------------------------

def _draw_bom_table(
    msp: Any,
    layout: UnifiedLayout,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
) -> None:
    """
    Draw a Bill-of-Materials table to the right of the plan.
    Columns: No. | Kind | Label | W (mm) | D (mm) | H (mm) | Qty
    Fixtures are grouped by (kind, label) and counted.
    """
    if not layout.fixtures:
        return

    # Aggregate
    from collections import Counter
    key_fn = lambda f: (f.kind or "generic", (f.label or "")[:40])
    counts: Counter = Counter(key_fn(f) for f in layout.fixtures)
    # Build rows: pick first matching fixture for dimensions
    dim_map: dict[tuple, tuple] = {}
    for f in layout.fixtures:
        k = key_fn(f)
        if k not in dim_map:
            dim_map[k] = (f.width_mm or 0, f.depth_mm or 0, f.height_mm or 0)

    rows = []
    for i, ((kind, label), qty) in enumerate(sorted(counts.items()), start=1):
        w, d, h = dim_map.get((kind, label), (0, 0, 0))
        rows.append((i, kind, label, w, d, h, qty))

    # Table geometry
    col_widths = [120, 300, 800, 250, 250, 250, 120]  # mm
    row_height = 180.0
    header_height = 220.0
    table_w = sum(col_widths)
    table_h = header_height + row_height * len(rows) + row_height  # +1 for header

    tx0 = maxx + _DIM_OFFSET * 5
    ty0 = maxy  # top-left of table

    headers = ["#", "KIND", "LABEL / MODEL", "W mm", "D mm", "H mm", "QTY"]

    # Outer border
    msp.add_lwpolyline(
        [
            Vec2(tx0, ty0),
            Vec2(tx0 + table_w, ty0),
            Vec2(tx0 + table_w, ty0 - table_h),
            Vec2(tx0, ty0 - table_h),
        ],
        close=True,
        dxfattribs={"layer": _LAYER_TITLE, "lineweight": 50},
    )

    # Title row
    _add_text(
        msp,
        "FIXTURE SCHEDULE / BOM",
        (tx0 + table_w / 2, ty0 - header_height / 2),
        _TEXT_HEIGHT_FIX * 1.1,
        _LAYER_TITLE,
    )
    msp.add_line(
        start=(tx0, ty0 - header_height),
        end=(tx0 + table_w, ty0 - header_height),
        dxfattribs={"layer": _LAYER_TITLE},
    )

    # Column headers
    cx_acc = tx0
    for i, (hdr, cw) in enumerate(zip(headers, col_widths)):
        _add_text(
            msp,
            hdr,
            (cx_acc + cw / 2, ty0 - header_height - row_height / 2),
            _TEXT_HEIGHT_FIX * 0.85,
            _LAYER_TITLE,
        )
        if i < len(col_widths) - 1:
            msp.add_line(
                start=(cx_acc + cw, ty0),
                end=(cx_acc + cw, ty0 - table_h),
                dxfattribs={"layer": _LAYER_TITLE},
            )
        cx_acc += cw

    # Header separator
    msp.add_line(
        start=(tx0, ty0 - header_height - row_height),
        end=(tx0 + table_w, ty0 - header_height - row_height),
        dxfattribs={"layer": _LAYER_TITLE},
    )

    # Data rows
    for row_idx, (num, kind, label, w, d, h, qty) in enumerate(rows):
        ry = ty0 - header_height - row_height - row_idx * row_height
        cells = [
            str(num),
            kind[:20],
            label[:50],
            f"{w:.0f}" if w else "—",
            f"{d:.0f}" if d else "—",
            f"{h:.0f}" if h else "—",
            str(qty),
        ]
        cx_acc = tx0
        for cell, cw in zip(cells, col_widths):
            _add_text(
                msp,
                cell,
                (cx_acc + cw / 2, ry - row_height / 2),
                _TEXT_HEIGHT_FIX * 0.75,
                _LAYER_ANNOT,
            )
            cx_acc += cw
        # Row separator
        msp.add_line(
            start=(tx0, ry - row_height),
            end=(tx0 + table_w, ry - row_height),
            dxfattribs={"layer": _LAYER_TITLE, "lineweight": 13},
        )


# ---------------------------------------------------------------------------
# Section I — Title block (IS/ISO A1 style)
# ---------------------------------------------------------------------------

def _draw_title_block(
    msp: Any,
    layout: UnifiedLayout,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    project_meta: dict,
) -> None:
    """
    Standard title block at bottom-right of drawing.
    Fields: Project, Client, Drawing Title, Scale, Units, Drawing No., Date, Revision.
    """
    tb_w = 3000.0
    tb_h = 600.0
    tb_x = maxx - tb_w
    tb_y = miny - _DIM_OFFSET * 4

    # Outer border
    msp.add_lwpolyline(
        [
            Vec2(tb_x, tb_y),
            Vec2(tb_x + tb_w, tb_y),
            Vec2(tb_x + tb_w, tb_y - tb_h),
            Vec2(tb_x, tb_y - tb_h),
        ],
        close=True,
        dxfattribs={"layer": _LAYER_TITLE, "lineweight": 70},
    )

    # Vertical divider at 60% width
    div_x = tb_x + tb_w * 0.6
    msp.add_line(
        start=(div_x, tb_y),
        end=(div_x, tb_y - tb_h),
        dxfattribs={"layer": _LAYER_TITLE},
    )

    # Left side: project + client
    client = str(project_meta.get("client") or project_meta.get("project_name") or "—")[:60]
    drawing_title = str(project_meta.get("drawing_title") or "FLOOR PLAN")[:60]
    _add_text(msp, client, (tb_x + tb_w * 0.3, tb_y - tb_h * 0.28), _TEXT_HEIGHT_FIX * 1.2, _LAYER_TITLE)
    _add_text(msp, drawing_title, (tb_x + tb_w * 0.3, tb_y - tb_h * 0.65), _TEXT_HEIGHT_FIX, _LAYER_TITLE)

    # Right side: metadata grid
    units = layout.units or "mm"
    scale_str = str(project_meta.get("scale") or "1:100")
    drg_no = str(project_meta.get("drawing_number") or "PF-001")
    revision = str(project_meta.get("revision") or "R0")

    right_fields = [
        ("SCALE", scale_str),
        ("UNITS", units.upper()),
        ("DRG NO.", drg_no),
        ("REV", revision),
    ]
    col_w = (tb_w - tb_w * 0.6) / 2
    row_h = tb_h / len(right_fields)
    for i, (key, val) in enumerate(right_fields):
        ry = tb_y - i * row_h
        # Key label (small, left-aligned in cell)
        _add_text(msp, key, (div_x + col_w * 0.25, ry - row_h * 0.28), _TEXT_HEIGHT_FIX * 0.65, _LAYER_TITLE)
        # Value (larger, centred in cell)
        _add_text(msp, val, (div_x + col_w * 0.75, ry - row_h * 0.68), _TEXT_HEIGHT_FIX * 0.9, _LAYER_TITLE)
        # Cell separator
        if i > 0:
            msp.add_line(
                start=(div_x, ry),
                end=(tb_x + tb_w, ry),
                dxfattribs={"layer": _LAYER_TITLE, "lineweight": 13},
            )
        # Vertical mid-divider
        msp.add_line(
            start=(div_x + col_w, tb_y),
            end=(div_x + col_w, tb_y - tb_h),
            dxfattribs={"layer": _LAYER_TITLE},
        )


# ---------------------------------------------------------------------------
# Section J — Scale bar + North arrow
# ---------------------------------------------------------------------------

def _draw_scale_bar(
    msp: Any,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    units: str,
) -> None:
    """
    Graphic scale bar: 5 divisions of 1000 mm each (= 5 m total).
    Placed bottom-left of drawing.
    """
    bar_x = minx
    bar_y = miny - _DIM_OFFSET * 4.5
    div_len = 1000.0  # 1 m per division in mm
    n_divs = 5
    bar_h = 80.0

    for i in range(n_divs):
        x0 = bar_x + i * div_len
        x1 = x0 + div_len
        fill = 7 if i % 2 == 0 else 0  # alternating white/black
        msp.add_solid(
            points=[(x0, bar_y), (x1, bar_y), (x0, bar_y - bar_h), (x1, bar_y - bar_h)],
            dxfattribs={"layer": _LAYER_SCALE, "color": fill},
        )
        msp.add_lwpolyline(
            [Vec2(x0, bar_y), Vec2(x1, bar_y), Vec2(x1, bar_y - bar_h), Vec2(x0, bar_y - bar_h)],
            close=True,
            dxfattribs={"layer": _LAYER_SCALE},
        )
        # Tick label
        label = f"{i}m" if units == "mm" else f"{i * div_len:.0f}"
        _add_text(msp, label, (x0, bar_y + 60), _TEXT_HEIGHT_FIX * 0.7, _LAYER_SCALE)

    _add_text(msp, f"{n_divs}m", (bar_x + n_divs * div_len, bar_y + 60), _TEXT_HEIGHT_FIX * 0.7, _LAYER_SCALE)
    _add_text(msp, f"SCALE BAR ({units.upper()})", (bar_x + n_divs * div_len / 2, bar_y - bar_h - 80),
              _TEXT_HEIGHT_FIX * 0.65, _LAYER_SCALE)

    # North arrow — to the right of scale bar
    na_x = bar_x + n_divs * div_len + 400
    na_y = bar_y - bar_h / 2
    arrow_len = 300.0
    # Shaft
    msp.add_line(
        start=(na_x, na_y - arrow_len / 2),
        end=(na_x, na_y + arrow_len / 2),
        dxfattribs={"layer": _LAYER_SCALE},
    )
    # Arrowhead (solid triangle)
    tip = (na_x, na_y + arrow_len / 2)
    left = (na_x - 60, na_y + arrow_len / 2 - 120)
    right = (na_x + 60, na_y + arrow_len / 2 - 120)
    msp.add_solid(
        points=[tip, left, tip, right],
        dxfattribs={"layer": _LAYER_SCALE, "color": 7},
    )
    _add_text(msp, "N", (na_x, na_y + arrow_len / 2 + 100), _TEXT_HEIGHT_FIX * 1.1, _LAYER_SCALE)


# ---------------------------------------------------------------------------
# Section K — Sheet-sourced dimension rendering (BOQ-accurate)
# ---------------------------------------------------------------------------

def _draw_sheet_dimensions(msp: Any, layout: UnifiedLayout, dim_style: str) -> int:
    """
    Render dimension entities from layout.meta['svg_dimensions'] — the precise
    architectural chains computed by the sheet parser directly from BOQ data.

    Each entry in svg_dimensions:
        x1, y1, x2, y2  — geometry endpoints
        nx, ny          — unit outward normal (direction the dim line is offset)
        offset_mm       — distance from geometry to dim line
        label           — display text (ignored; ezdxf measures automatically)

    Returns the number of dimension entities successfully rendered.
    """
    svg_dims = []
    if layout.meta:
        raw = layout.meta.get("svg_dimensions")
        if isinstance(raw, list):
            svg_dims = raw

    if not svg_dims:
        return 0

    count = 0
    for entry in svg_dims:
        try:
            x1 = float(entry["x1"])
            y1 = float(entry["y1"])
            x2 = float(entry["x2"])
            y2 = float(entry["y2"])
            nx = float(entry.get("nx", 0.0))
            ny = float(entry.get("ny", -1.0))
            offset = float(entry.get("offset_mm", _DIM_OFFSET))
        except (KeyError, TypeError, ValueError):
            continue

        # Skip degenerate segments
        if math.hypot(x2 - x1, y2 - y1) < 10:
            continue

        # Midpoint + outward normal → dim line base point
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        bx = mx + nx * offset
        by = my + ny * offset

        is_horizontal = abs(y2 - y1) < 1.0
        is_vertical = abs(x2 - x1) < 1.0

        try:
            if is_horizontal:
                msp.add_linear_dim(
                    base=(bx, by),
                    p1=(x1, y1),
                    p2=(x2, y2),
                    dimstyle=dim_style,
                    dxfattribs={"layer": _LAYER_DIMS},
                ).render()
            elif is_vertical:
                msp.add_linear_dim(
                    base=(bx, by),
                    p1=(x1, y1),
                    p2=(x2, y2),
                    angle=90,
                    dimstyle=dim_style,
                    dxfattribs={"layer": _LAYER_DIMS},
                ).render()
            else:
                msp.add_aligned_dim(
                    p1=(x1, y1),
                    p2=(x2, y2),
                    dist=offset,
                    dimstyle=dim_style,
                    dxfattribs={"layer": _LAYER_DIMS},
                ).render()
            count += 1
        except Exception as exc:
            logger.debug("sheet dim skipped (%s,%s)→(%s,%s): %s", x1, y1, x2, y2, exc)

    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def layout_to_dxf(layout: UnifiedLayout) -> ezdxf.document.Drawing:
    """
    Convert a UnifiedLayout to an ezdxf Drawing object.
    Returns the document — caller decides where to save it.

    All geometry is in the layout's native units (mm by default).
    The DXF is R2018 format for maximum AutoCAD / BricsCAD compatibility.
    """
    global _text_style_name

    doc = ezdxf.new(dxfversion="R2018")
    msp = doc.modelspace()

    _setup_layers(doc)
    _text_style_name = _setup_text_style(doc)
    dim_style = _setup_dimstyle(doc)

    # Load all standard ACAD linetypes (DASHED, HIDDEN, CENTER, PHANTOM, …)
    try:
        _ezdxf_setup_linetypes(doc)
    except Exception as exc:
        logger.debug("setup_linetypes failed: %s", exc)

    minx, miny, maxx, maxy = _bounds(layout)

    # --- Geometry layers ---
    _draw_walls(msp, layout)
    _draw_rooms(msp, layout)
    _draw_fixtures(msp, layout, dim_style)
    _draw_openings(msp, layout, dim_style)

    # --- Dimensioning ---
    _draw_overall_dimensions(msp, layout, dim_style, minx, miny, maxx, maxy)
    # Use BOQ-sourced dims when available; fall back to coordinate-snapshot chain
    sheet_dim_count = _draw_sheet_dimensions(msp, layout, dim_style)
    if sheet_dim_count == 0:
        _draw_partition_dimensions(msp, layout, dim_style, minx, miny, maxx, maxy)

    # --- Annotations ---
    _draw_elevation_markers(msp, layout, minx, miny, maxx, maxy)
    _draw_bom_table(msp, layout, minx, miny, maxx, maxy)

    # --- Drawing furniture ---
    project_meta: dict = {}
    if layout.meta:
        pm = layout.meta.get("projectmeta")
        if isinstance(pm, dict):
            project_meta = pm

    _draw_title_block(msp, layout, minx, miny, maxx, maxy, project_meta)
    _draw_scale_bar(msp, minx, miny, maxx, maxy, layout.units)

    # Set model-space extents for AutoCAD ZOOM EXTENTS
    try:
        doc.header["$EXTMIN"] = (minx - 2000, miny - 2000, 0)
        doc.header["$EXTMAX"] = (maxx + 5000, maxy + 1000, 0)
        doc.header["$INSUNITS"] = 4  # 4 = millimetres
        doc.header["$MEASUREMENT"] = 1  # 1 = metric
    except Exception as exc:
        logger.debug("Header vars not set: %s", exc)

    return doc


def export_layout_to_dxf(layout: UnifiedLayout, output_path: Path | str) -> Path:
    """
    Render layout to DXF and write to *output_path*.
    Returns the resolved Path.
    """
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = layout_to_dxf(layout)
    doc.saveas(str(p))
    logger.info("DXF saved: %s", p)
    return p
