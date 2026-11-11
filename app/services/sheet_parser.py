"""PDF / Excel → tabular room names and dimensions."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
import re
from typing import Any

import pandas as pd
import pdfplumber

_LABEL_MAX_CHARS = 120   # max length for any room/fixture/opening label string

# Unified CSV: column name aliases that route rows to the same logical sheets as Excel tabs.
# Order matters: real sheets often include a data column named "layer" (e.g. wall layer); that
# must not win over explicit routers like _sheet / sheet.
_SHEET_ROUTING_COLUMN_PRIORITY: tuple[str, ...] = (
    "_sheet",
    "sheet",
    "worksheet",
    "tab",
    "table",
    "source_sheet",
    "layer",
)

# Sheets that are catalog/reference only — no L×W floor rectangles here.
_SKIP_ROOM_POLYGON_SHEETS = frozenset(
    {
        "fixture_schedule",
        "fixtures",
        "projectmeta",
        "walls",
        "dimensions",
        "lfd_models",
        "lfd_model_sizes",
        "openings",
        "wall_segments_top",
        "wall_segments_bottom",
        "summary",
    }
)

# Fixture centres only from these sheets (avoids treating stray coordinates as fixtures).
_FIXTURE_SHEETS = frozenset({"fixture_schedule", "fixtures"})


def _normalize_unit(unit: str | None) -> str:
    if not unit:
        return "mm"
    u = unit.strip().lower()
    if u in ("m", "meter", "meters", "metres"):
        return "m"
    if u in ("mm", "millimeter", "millimeters", "millimetres"):
        return "mm"
    if u in ("ft", "feet", "'"):
        return "ft"
    if u in ("in", "inch", "inches", '"'):
        return "in"
    return u


_LXW_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(?:mm|m|ft|in|cm)?\s*$"
)


def _parse_lxw(val: Any) -> tuple[float, float] | None:
    """Parse '600x600mm', '1200×600', '600 x 600 mm' → (length, width) floats."""
    if val is None:
        return None
    m = _LXW_RE.match(str(val).strip())
    return (float(m.group(1)), float(m.group(2))) if m else None


def _float_cell(lower: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        if k not in lower:
            continue
        v = lower[k]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _origin_xy(lower: dict[str, Any]) -> tuple[float, float]:
    ox = _float_cell(lower, "origin_x", "origin_x_mm", "ox", "x0", "anchor_x") or 0.0
    oy = _float_cell(lower, "origin_y", "origin_y_mm", "oy", "y0", "anchor_y") or 0.0
    return ox, oy


def _rect_polygon(L: float, W: float, ox: float, oy: float) -> list[list[float]]:
    return [
        [ox, oy],
        [ox + float(L), oy],
        [ox + float(L), oy + float(W)],
        [ox, oy + float(W)],
    ]


def _bbox(poly: list[list[float]]) -> tuple[float, float, float, float] | None:
    if len(poly) < 3:
        return None
    xs = [float(p[0]) for p in poly if len(p) >= 2]
    ys = [float(p[1]) for p in poly if len(p) >= 2]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _infer_touching_partition_walls(rooms: list[dict[str, Any]], tol: float = 2.0) -> list[dict[str, float]]:
    """Vertical segments shared by two axis-aligned room rectangles → partition walls."""
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for r in rooms:
        poly = r.get("polygon") or []
        bb = _bbox(poly)
        if bb is None:
            continue
        boxes.append((str(r.get("name") or ""), bb))

    walls: list[dict[str, float]] = []
    for i, (na, a) in enumerate(boxes):
        minxa, minya, maxxa, maxya = a
        for nb, b in boxes[i + 1 :]:
            minxb, minyb, maxxb, maxyb = b
            # Vertical seam: right edge of one meets left edge of other
            if abs(maxxa - minxb) <= tol:
                x = (maxxa + minxb) / 2.0
                y0 = max(minya, minyb)
                y1 = min(maxya, maxyb)
                if y1 > y0 + tol:
                    walls.append({"x1": x, "y1": y0, "x2": x, "y2": y1})
            elif abs(maxxb - minxa) <= tol:
                x = (maxxb + minxa) / 2.0
                y0 = max(minya, minyb)
                y1 = min(maxya, maxyb)
                if y1 > y0 + tol:
                    walls.append({"x1": x, "y1": y0, "x2": x, "y2": y1})

    return walls


def _fmt_mm_label(v: float) -> str:
    if abs(v - round(v)) < 0.05:
        return str(int(round(v)))
    return str(round(v, 1))


def _normalize_sheet_tag(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _excel_file_to_tables(path: Path) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Load workbook into tag → DataFrame; labels map tag → original sheet title."""
    xl = pd.ExcelFile(path)
    tables: dict[str, pd.DataFrame] = {}
    labels: dict[str, str] = {}
    for sheet in xl.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet)
        df = df.dropna(how="all")
        tag = _normalize_sheet_tag(sheet)
        base = tag
        n = 2
        while tag in tables:
            tag = f"{base}_{n}"
            n += 1
        tables[tag] = df
        labels[tag] = sheet.strip()
    return tables, labels


def _detect_sheet_routing_column(df: pd.DataFrame) -> str | None:
    cols = {str(c).lower().strip(): c for c in df.columns}
    for key in _SHEET_ROUTING_COLUMN_PRIORITY:
        if key in cols:
            return cols[key]
    return None


def _strip_planforge_prefix(tag: str) -> str:
    t = tag.strip().lower().replace(" ", "_")
    if t.startswith("planforge_"):
        return t[len("planforge_") :]
    return t


def _read_explicit_walls_df(df: pd.DataFrame) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for _, row in df.iterrows():
        lower = {str(k).lower().strip(): v for k, v in row.items() if k is not None and str(k).strip()}
        x1 = _float_cell(lower, "x1_mm", "x1")
        y1 = _float_cell(lower, "y1_mm", "y1")
        x2 = _float_cell(lower, "x2_mm", "x2")
        y2 = _float_cell(lower, "y2_mm", "y2")
        if x1 is None or y1 is None or x2 is None or y2 is None:
            continue
        out.append({"x1": float(x1), "y1": float(y1), "x2": float(x2), "y2": float(y2)})
    return out


def _read_openings_df(df: pd.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        lower = {str(k).lower().strip(): v for k, v in row.items() if k is not None and str(k).strip()}
        x1 = _float_cell(lower, "x1_mm", "x1")
        y1 = _float_cell(lower, "y1_mm", "y1")
        x2 = _float_cell(lower, "x2_mm", "x2")
        y2 = _float_cell(lower, "y2_mm", "y2")
        if x1 is None or y1 is None or x2 is None or y2 is None:
            continue
        label = lower.get("label")
        kind = lower.get("type") or lower.get("opening_type") or "opening"
        out.append(
            {
                "kind": str(kind),
                "x1": float(x1),
                "y1": float(y1),
                "x2": float(x2),
                "y2": float(y2),
                "label": None if label is None or (isinstance(label, float) and pd.isna(label)) else str(label),
            }
        )
    return out


def _read_project_meta_df(df: pd.DataFrame) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for _, row in df.iterrows():
        lower = {str(k).lower().strip(): v for k, v in row.items() if k is not None and str(k).strip()}
        fk = lower.get("field")
        fv = lower.get("value")
        if fk is None or (isinstance(fk, float) and pd.isna(fk)):
            continue
        ks = str(fk).strip()
        if ks.upper() == "FIELD" and fv is not None and str(fv).strip().upper() == "VALUE":
            continue
        if fv is None or (isinstance(fv, float) and pd.isna(fv)):
            meta[ks] = None
        else:
            meta[ks] = fv
    return meta


def _read_dimension_callouts_df(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item = row.dropna().to_dict()
        if not item:
            continue
        lower = {str(k).lower().strip(): v for k, v in item.items()}
        vid = lower.get("dim_id") or lower.get("id")
        val = _float_cell(lower, "value_mm", "mm", "value")
        if val is None:
            continue
        cin = lower.get("color_in_dwg") or lower.get("color")
        rows_out.append(
            {
                "id": str(vid) if vid is not None and not (isinstance(vid, float) and pd.isna(vid)) else None,
                "axis": lower.get("axis"),
                "value_mm": float(val),
                "notes": lower.get("notes"),
                "color_in_dwg": str(cin).strip() if cin is not None and str(cin).strip() else None,
            }
        )
    return rows_out


def _read_explicit_walls_tables(tables: dict[str, pd.DataFrame]) -> list[dict[str, float]]:
    df = tables.get("walls")
    if df is None or df.empty:
        return []
    return _read_explicit_walls_df(df)


def _read_openings_tables(tables: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    df = tables.get("openings")
    if df is None or df.empty:
        return []
    return _read_openings_df(df)


def _read_project_meta_tables(tables: dict[str, pd.DataFrame]) -> dict[str, Any]:
    df = tables.get("projectmeta")
    if df is None or df.empty:
        return {}
    return _read_project_meta_df(df)


def _read_dimension_callouts_tables(tables: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    df = tables.get("dimensions")
    if df is None or df.empty:
        return []
    return _read_dimension_callouts_df(df)


def _vendor_table_records(tables: dict[str, pd.DataFrame]) -> dict[str, list[dict[str, str]]]:
    """Catalog sheets (lfd_models, …) → meta.fixture_vendor_tables for fixture-layout SVG annex."""
    out: dict[str, list[dict[str, str]]] = {}

    def _col(lower_map: dict[str, str], *aliases: str) -> str | None:
        for a in aliases:
            k = a.lower().strip()
            if k in lower_map:
                return lower_map[k]
        return None

    for tag in ("lfd_models", "lfd_model_sizes"):
        df = tables.get(tag)
        if df is None or df.empty:
            continue
        df = df.copy()
        df.columns = [str(c).strip() for c in df.columns]
        lower_map = {str(c).lower().strip(): str(c) for c in df.columns}

        c_type = _col(lower_map, "type", "category", "display_type")
        c_diag = _col(lower_map, "diagonal_size", "diagonal", "size", "diagonal (inch)")
        c_model = _col(lower_map, "model_number", "model", "model_no")
        c_set = _col(
            lower_map,
            "set_dimension",
            "set_dim",
            "set_wxh_d",
            "set dimension (wxhxd)",
            "set dimension",
        )
        c_niche = _col(
            lower_map,
            "niche_sizes",
            "niche",
            "niche_wxh_d",
            "niche sizes (wxhxd)",
            "niche sizes",
        )
        # Unified wide CSV: repurpose room_name / name / length / label / kind
        fb_type = _col(lower_map, "room_name")
        fb_model = _col(lower_map, "name")
        fb_diag = _col(lower_map, "length")
        fb_set = _col(lower_map, "label")
        fb_niche = _col(lower_map, "kind")

        def cell(row: pd.Series, col: str | None) -> str:
            if not col or col not in row.index:
                return ""
            v = row[col]
            if pd.isna(v):
                return ""
            return str(v).strip()

        rows_out: list[dict[str, str]] = []
        for _, row in df.iterrows():
            typ = cell(row, c_type) or cell(row, fb_type)
            diag = cell(row, c_diag) or cell(row, fb_diag)
            model = cell(row, c_model) or cell(row, fb_model)
            set_d = cell(row, c_set) or cell(row, fb_set)
            niche = cell(row, c_niche) or cell(row, fb_niche)
            if not typ and not model and not diag:
                continue
            rows_out.append(
                {
                    "type": typ or "—",
                    "diagonal": diag or "—",
                    "model_number": model or "—",
                    "set_dims": set_d or "—",
                    "niche_dims": niche or "—",
                }
            )
        if rows_out:
            out[tag] = rows_out
    return out


def _dim_id_num(dim_id: Any) -> int | None:
    if dim_id is None:
        return None
    s = str(dim_id).strip().upper()
    if len(s) < 2 or s[0] != "D":
        return None
    try:
        return int(s[1:])
    except ValueError:
        return None


def _dim_style_for_row(d: dict[str, Any]) -> tuple[str, str]:
    col = str(d.get("color_in_dwg") or d.get("color") or "").upper()
    line = "#b91c1c" if "RED" in col else "#334155"
    fill = "#b91c1c" if "RED" in col else "#0f172a"
    return line, fill


def _horizontal_dim_chain_colored(
    xa: float,
    ya: float,
    xb: float,
    segments: list[tuple[float, str, str]],
    *,
    outward_up: bool,
    offset_mm: float,
) -> list[dict[str, Any]]:
    runs = [s[0] for s in segments]
    span = xb - xa
    total = sum(runs) if runs else 0.0
    scale_f = span / total if total > 1e-9 else 1.0
    nx, ny = ((0.0, 1.0) if outward_up else (0.0, -1.0))
    items: list[dict[str, Any]] = []
    cum = xa
    for i, rf in enumerate(runs):
        seg_len = float(rf) * scale_f
        x2 = cum + seg_len
        ln, ft = segments[i][1], segments[i][2]
        items.append(
            {
                "x1": cum,
                "y1": ya,
                "x2": x2,
                "y2": ya,
                "label": _fmt_mm_label(float(rf)),
                "nx": nx,
                "ny": ny,
                "dim_stroke": ln,
                "text_fill": ft,
                "offset_mm": offset_mm,
            }
        )
        cum = x2
    return items


def _vertical_dim_chain_colored(
    xa: float,
    ya: float,
    yb: float,
    segments: list[tuple[float, str, str]],
    *,
    outward_left: bool,
    offset_mm: float,
) -> list[dict[str, Any]]:
    runs = [s[0] for s in segments]
    span = yb - ya
    total = sum(runs) if runs else 0.0
    scale_f = span / total if total > 1e-9 else 1.0
    nx, ny = ((-1.0, 0.0) if outward_left else (1.0, 0.0))
    items: list[dict[str, Any]] = []
    cum = ya
    for i, rf in enumerate(runs):
        seg_len = float(rf) * scale_f
        y2 = cum + seg_len
        ln, ft = segments[i][1], segments[i][2]
        items.append(
            {
                "x1": xa,
                "y1": cum,
                "x2": xa,
                "y2": y2,
                "label": _fmt_mm_label(float(rf)),
                "nx": nx,
                "ny": ny,
                "dim_stroke": ln,
                "text_fill": ft,
                "offset_mm": offset_mm,
            }
        )
        cum = y2
    return items


def _overall_dim_segment(
    xa: float,
    ya: float,
    xb: float,
    label: str,
    ln: str,
    ft: str,
    *,
    outward_up: bool,
    offset_mm: float,
) -> list[dict[str, Any]]:
    nx, ny = ((0.0, 1.0) if outward_up else (0.0, -1.0))
    return [
        {
            "x1": xa,
            "y1": ya,
            "x2": xb,
            "y2": ya,
            "label": label,
            "nx": nx,
            "ny": ny,
            "dim_stroke": ln,
            "text_fill": ft,
            "offset_mm": offset_mm,
        }
    ]


def _build_svg_dimensions_from_dimension_callouts(
    callouts: list[dict[str, Any]],
    rooms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    xs: list[float] = []
    ys: list[float] = []
    for r in rooms:
        for pt in r.get("polygon") or []:
            if len(pt) >= 2:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
    if not xs:
        return []

    minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
    by_num: dict[int, dict[str, Any]] = {}
    for d in callouts:
        n = _dim_id_num(d.get("id"))
        if n is not None:
            by_num[n] = d

    items: list[dict[str, Any]] = []

    # Generic chains (for any well-formed dimension callouts).
    # These do not rely on "from_ref/to_ref" geometry; they simply render the provided value_mm
    # as a dimension string spanning the current bounding box.
    x_segments_any: list[tuple[int, float, str, str]] = []
    y_segments_any: list[tuple[int, float, str, str]] = []
    for d in callouts:
        axis = str(d.get("axis") or "").upper().strip()
        if axis not in ("X", "Y"):
            continue
        try:
            v = float(d["value_mm"])
        except (KeyError, TypeError, ValueError):
            continue
        ln, ft = _dim_style_for_row(d)
        n = _dim_id_num(d.get("id"))
        order = n if n is not None else 10_000
        if axis == "X":
            x_segments_any.append((order, v, ln, ft))
        else:
            y_segments_any.append((order, v, ln, ft))
    x_segments_any.sort(key=lambda t: t[0])
    y_segments_any.sort(key=lambda t: t[0])

    if x_segments_any:
        items.extend(
            _horizontal_dim_chain_colored(
                minx,
                maxy,
                maxx,
                [(v, ln, ft) for _, v, ln, ft in x_segments_any],
                outward_up=True,
                offset_mm=440.0,
            )
        )
    if y_segments_any:
        items.extend(
            _vertical_dim_chain_colored(
                minx,
                miny,
                maxy,
                [(v, ln, ft) for _, v, ln, ft in y_segments_any],
                outward_left=True,
                offset_mm=520.0,
            )
        )

    top_segments: list[tuple[float, str, str]] = []
    for n in range(1, 9):
        d = by_num.get(n)
        if not d or str(d.get("axis") or "").upper() != "X":
            continue
        v = float(d["value_mm"])
        ln, ft = _dim_style_for_row(d)
        top_segments.append((v, ln, ft))
    if top_segments:
        items.extend(
            _horizontal_dim_chain_colored(
                minx, maxy, maxx, top_segments, outward_up=True, offset_mm=440.0
            )
        )

    d9 = by_num.get(9)
    if d9 and str(d9.get("axis") or "").upper() == "X":
        ln, ft = _dim_style_for_row(d9)
        lv = _fmt_mm_label(float(d9["value_mm"]))
        items.extend(
            _overall_dim_segment(
                minx, maxy, maxx, lv, ln, ft, outward_up=True, offset_mm=560.0
            )
        )

    bot_segments: list[tuple[float, str, str]] = []
    for n in range(10, 22):
        d = by_num.get(n)
        if not d or str(d.get("axis") or "").upper() != "X":
            continue
        v = float(d["value_mm"])
        ln, ft = _dim_style_for_row(d)
        bot_segments.append((v, ln, ft))
    if bot_segments:
        items.extend(
            _horizontal_dim_chain_colored(
                minx, miny, maxx, bot_segments, outward_up=False, offset_mm=440.0
            )
        )

    d22 = by_num.get(22)
    if d22 and str(d22.get("axis") or "").upper() == "X":
        ln, ft = _dim_style_for_row(d22)
        lv = _fmt_mm_label(float(d22["value_mm"]))
        notes = str(d22.get("notes") or "").strip()
        label = f"{lv}" + (f" · {notes}" if notes else "")
        items.extend(
            _overall_dim_segment(
                minx, miny, maxx, label, ln, ft, outward_up=False, offset_mm=580.0
            )
        )

    d23 = by_num.get(23)
    shell_h = maxy - miny
    if d23 and str(d23.get("axis") or "").upper() == "Y":
        ln, ft = _dim_style_for_row(d23)
        items.append(
            {
                "x1": minx,
                "y1": miny,
                "x2": minx,
                "y2": maxy,
                "label": _fmt_mm_label(float(d23.get("value_mm") or shell_h)),
                "nx": -1.0,
                "ny": 0.0,
                "dim_stroke": ln,
                "text_fill": ft,
                "offset_mm": 500.0,
            }
        )
    else:
        items.append(
            {
                "x1": minx,
                "y1": miny,
                "x2": minx,
                "y2": maxy,
                "label": _fmt_mm_label(shell_h),
                "nx": -1.0,
                "ny": 0.0,
            }
        )

    return items


def _read_wall_runs_tables(tables: dict[str, pd.DataFrame], needle: str) -> list[float]:
    nt = needle.lower().replace(" ", "_")
    df: pd.DataFrame | None = None
    for tag, dfc in tables.items():
        if nt in tag:
            df = dfc
            break
    if df is None or df.empty:
        return []
    df = df.dropna(how="all")
    lower_cols = {str(c).lower().strip(): c for c in df.columns}
    col = None
    for key in ("run_mm", "length_mm", "segment_mm", "mm", "length"):
        if key in lower_cols:
            col = lower_cols[key]
            break
    if col is None:
        return []
    runs: list[float] = []
    for v in df[col]:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        try:
            runs.append(float(v))
        except (TypeError, ValueError):
            continue
    return runs


def _read_wall_runs(path: Path, needle: str) -> list[float]:
    tables, _ = _excel_file_to_tables(path)
    return _read_wall_runs_tables(tables, needle)


def _horizontal_dim_chain(
    xa: float,
    ya: float,
    xb: float,
    runs: list[float],
    *,
    outward_up: bool,
) -> list[dict[str, Any]]:
    span = xb - xa
    total = sum(runs) if runs else 0.0
    scale_f = span / total if total > 1e-9 else 1.0
    nx, ny = ((0.0, 1.0) if outward_up else (0.0, -1.0))
    items: list[dict[str, Any]] = []
    cum = xa
    for r in runs:
        rf = float(r)
        seg_len = rf * scale_f
        x2 = cum + seg_len
        items.append(
            {
                "x1": cum,
                "y1": ya,
                "x2": x2,
                "y2": ya,
                "label": _fmt_mm_label(rf),
                "nx": nx,
                "ny": ny,
            }
        )
        cum = x2
    return items


def _build_svg_dimensions_from_tables(tables: dict[str, pd.DataFrame], rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    xs: list[float] = []
    ys: list[float] = []
    for r in rooms:
        poly = r.get("polygon") or []
        for pt in poly:
            if len(pt) >= 2:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
    if not xs:
        return []
    minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
    bottom_runs = _read_wall_runs_tables(tables, "wall_segments_bottom")
    top_runs = _read_wall_runs_tables(tables, "wall_segments_top")
    items: list[dict[str, Any]] = []
    items.extend(_horizontal_dim_chain(minx, miny, maxx, bottom_runs, outward_up=False))
    items.extend(_horizontal_dim_chain(minx, maxy, maxx, top_runs, outward_up=True))
    shell_h = maxy - miny
    items.append(
        {
            "x1": minx,
            "y1": miny,
            "x2": minx,
            "y2": maxy,
            "label": _fmt_mm_label(shell_h),
            "nx": -1.0,
            "ny": 0.0,
        }
    )
    return items


def _build_svg_dimensions_from_excel(path: Path, rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tables, _ = _excel_file_to_tables(path)
    return _build_svg_dimensions_from_tables(tables, rooms)


def _parse_workbook_tables(
    tables: dict[str, pd.DataFrame],
    *,
    sheet_labels: dict[str, str] | None = None,
    sheet_order: list[str] | None = None,
    format_hint: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    units_guess = "mm"

    tag_iter = sheet_order if sheet_order is not None else sorted(tables.keys())

    for sheet_tag in tag_iter:
        df = tables.get(sheet_tag)
        if df is None or df.empty:
            continue
        df = df.dropna(how="all")
        sheet_display = (sheet_labels or {}).get(sheet_tag, sheet_tag)

        for _, row in df.iterrows():
            item = row.dropna().to_dict()
            if not item:
                continue
            lower = {str(k).lower().strip(): v for k, v in item.items()}
            name = None
            for key in (
                "room_name", "room", "name", "space", "area name",
                "item description", "description", "item", "desc", "label",
            ):
                if key in lower:
                    name = lower[key]
                    break
            if name is None and item:
                name = next(iter(item.values()))
            if name is None or (isinstance(name, float) and pd.isna(name)):
                continue

            dim_a = (
                _float_cell(lower, "length", "l", "length_mm", "width_mm")
                if sheet_tag not in _SKIP_ROOM_POLYGON_SHEETS
                else None
            )
            dim_b = (
                _float_cell(lower, "depth_mm", "depth", "width", "w")
                if sheet_tag not in _SKIP_ROOM_POLYGON_SHEETS
                else None
            )

            # Fallback: combined "Dimension" column — e.g. "600x600mm", "1200×600"
            if (dim_a is None or dim_b is None) and sheet_tag not in _SKIP_ROOM_POLYGON_SHEETS:
                for dim_key in ("dimension", "dimensions", "dim", "size", "sizes"):
                    if dim_key in lower:
                        parsed_lxw = _parse_lxw(lower[dim_key])
                        if parsed_lxw:
                            if dim_a is None:
                                dim_a = parsed_lxw[0]
                            if dim_b is None:
                                dim_b = parsed_lxw[1]
                            break

            if sheet_tag == "fixture_schedule":
                dim_a = _float_cell(lower, "length", "l", "length_mm")
                dim_b = _float_cell(lower, "width", "w", "depth_mm")

            area = lower.get("area") or lower.get("sqm") or lower.get("sqft")
            u = lower.get("unit") or lower.get("units")
            if u:
                units_guess = _normalize_unit(str(u))

            ox, oy = _origin_xy(lower)
            cx = _float_cell(lower, "center_x_mm", "center_x", "fixture_x", "cx_mm")
            cy = _float_cell(lower, "center_y_mm", "center_y", "fixture_y", "cy_mm")

            rows.append(
                {
                    "sheet": sheet_display,
                    "sheet_tag": sheet_tag,
                    "name": str(name),
                    "length": dim_a,
                    "width": dim_b,
                    "area": float(area) if area is not None and pd.notna(area) else None,
                    "origin_x": ox,
                    "origin_y": oy,
                    "center_x": cx,
                    "center_y": cy,
                    "lower": lower,
                }
            )

    rooms: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []

    for r in rows:
        name = r["name"]
        if not name:
            continue
        st = r["sheet_tag"]
        L, W = r.get("length"), r.get("width")
        ox, oy = r.get("origin_x") or 0.0, r.get("origin_y") or 0.0
        lower = r.get("lower") or {}

        if st in _FIXTURE_SHEETS:
            cx, cy = r.get("center_x"), r.get("center_y")
            if cx is not None and cy is not None:
                lab = lower.get("label")
                if lab is not None and not (isinstance(lab, float) and pd.isna(lab)):
                    label_txt = str(lab)[:_LABEL_MAX_CHARS]
                else:
                    label_txt = str(name)[:_LABEL_MAX_CHARS]
                rot = _float_cell(lower, "rotation_deg", "rot_deg", "rotation") or 0.0
                fixtures.append(
                    {
                        "kind": str(lower.get("kind") or "fixture"),
                        "x": float(cx),
                        "y": float(cy),
                        "label": label_txt,
                        "width_mm": _float_cell(lower, "width_mm", "w_mm"),
                        "depth_mm": _float_cell(lower, "depth_mm", "d_mm"),
                        "height_mm": _float_cell(lower, "height_mm", "h_mm"),
                        "rotation_deg": float(rot),
                    }
                )

        if st in _SKIP_ROOM_POLYGON_SHEETS:
            continue
        if st == "fixture_schedule":
            continue

        if L is None or W is None:
            continue

        poly = _rect_polygon(L, W, ox, oy)
        rooms.append(
            {
                "name": name,
                "area": r.get("area"),
                "polygon": poly,
                "source_row": {k: v for k, v in r.items() if k != "lower"},
            }
        )

    walls_inferred = _infer_touching_partition_walls(rooms)
    explicit_walls = _read_explicit_walls_tables(tables)
    walls_final = explicit_walls if explicit_walls else walls_inferred

    openings_out = _read_openings_tables(tables)
    project_meta = _read_project_meta_tables(tables)
    dim_callouts = _read_dimension_callouts_tables(tables)

    has_wall_seg_runs = bool(
        _read_wall_runs_tables(tables, "wall_segments_bottom")
        or _read_wall_runs_tables(tables, "wall_segments_top")
    )
    if has_wall_seg_runs:
        svg_dimensions = _build_svg_dimensions_from_tables(tables, rooms)
    elif dim_callouts:
        svg_dimensions = _build_svg_dimensions_from_dimension_callouts(dim_callouts, rooms)
        if not svg_dimensions:
            svg_dimensions = _build_svg_dimensions_from_tables(tables, rooms)
    else:
        svg_dimensions = _build_svg_dimensions_from_tables(tables, rooms)

    if sheet_order is not None and sheet_labels:
        meta_names = [sheet_labels.get(t, t) for t in sheet_order]
    elif sheet_labels:
        meta_names = [sheet_labels[k] for k in sorted(sheet_labels.keys())]
    else:
        meta_names = sorted(tables.keys())

    meta_out: dict[str, Any] = {
        "sheets": meta_names,
        "svg_dimensions": svg_dimensions,
        "svg_dimension_offset_mm": 380.0,
        "projectmeta": project_meta,
        "dimension_callouts": dim_callouts,
    }
    if format_hint:
        meta_out["sheet_bundle_format"] = format_hint

    vendor_tabs = _vendor_table_records(tables)
    if vendor_tabs:
        meta_out["fixture_vendor_tables"] = vendor_tabs

    return {
        "source": "sheet",
        "units": units_guess,
        "walls": walls_final,
        "rooms": rooms,
        "fixtures": fixtures,
        "openings": openings_out,
        "tables": [],
        "meta": meta_out,
    }


# ---------------------------------------------------------------------------
# Generic unstructured BOQ Excel parser
# (handles vendor BOQ exports and any ad-hoc fixture schedule format)
# ---------------------------------------------------------------------------

# Sheet names that suggest a fixture BOQ (case-insensitive substring match)
_BOQ_SHEET_HINTS: tuple[str, ...] = (
    "boq",
    "fixture",
    "equipment",
    "schedule",
    "item list",
    "bill of quantity",
    "bill of material",
)

# Semantic keyword groups for column auto-detection.
# Each group: list of keywords — if ANY keyword appears in a normalised column
# name the column is assigned to that semantic field.
_BOQ_COL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "label":  ("name", "desc", "item", "fixture", "product", "model",
               "part", "drawing", "code", "ref"),
    "width":  ("width", "breadth", "wide", "w"),
    "depth":  ("depth", "length", "long", "run", "d", "l"),
    "height": ("height", "tall", "elev", "h"),
    "qty":    ("qty", "quantity", "count", "nos", "no", "pcs", "piece", "number"),
    "uom":    ("uom", "unit", "measure"),
    "xpos":   ("x_pos", "pos_x", "x pos", "origin_x", "cx", "center_x"),
    "ypos":   ("y_pos", "pos_y", "y pos", "origin_y", "cy", "center_y"),
}

# Unit keywords that may appear in column headers like "WIDTH (FT)" or "DEPTH_MM"
_UNIT_IN_HEADER: tuple[tuple[str, str], ...] = (
    ("_mm",  "mm"), ("(mm)",  "mm"), (" mm",  "mm"),
    ("_m",   "m"),  ("(m)",   "m"),  (" m",   "m"),
    ("_ft",  "ft"), ("(ft)",  "ft"), (" ft",  "ft"), ("feet", "ft"),
    ("_in",  "in"), ("(in)",  "in"), (" in",  "in"), ("inch", "in"),
    ("_cm",  "cm"), ("(cm)",  "cm"), (" cm",  "cm"),
)

_TO_MM: dict[str, float] = {
    "mm": 1.0,
    "cm": 10.0,
    "m":  1000.0,
    "ft": 304.8,
    "in": 25.4,
}


def _boq_detect_unit_from_header(col: str) -> str | None:
    """Extract unit embedded in a column header string, e.g. 'Width (FT)' → 'ft'."""
    c = str(col).lower()
    for suffix, unit in _UNIT_IN_HEADER:
        if suffix in c:
            return unit
    return None


def _boq_auto_unit_from_values(values: list[float]) -> str:
    """Guess mm/ft/m from value distribution (median heuristic)."""
    pos = [v for v in values if v > 0]
    if not pos:
        return "mm"
    median = sorted(pos)[len(pos) // 2]
    if median < 30:      # e.g. 1.5 → metres
        return "m"
    if median < 200:     # e.g. 6.0 → feet  (6 ft ≈ 1800 mm)
        return "ft"
    return "mm"          # anything ≥ 200 treated as mm already


def _boq_semantic_match(col_name: str, field: str) -> bool:
    """Return True if *col_name* contains any keyword for *field*."""
    col_norm = re.sub(r"[^a-z0-9 ]", " ", str(col_name).lower())
    tokens = set(col_norm.split())
    for kw in _BOQ_COL_KEYWORDS.get(field, ()):
        if len(kw) <= 2:
            # Short keywords (single letters like "l", "w", "d", "h") must match
            # as whole tokens only — substring matching causes "d" to match "description",
            # "dimension", "depth" all at once, producing wrong column assignments.
            if kw in tokens:
                return True
        else:
            if kw in tokens or any(kw in tok for tok in tokens):
                return True
    return False


def _boq_map_columns(
    columns: list[str],
) -> dict[str, str | None]:
    """
    Map semantic fields to the best-matching column name in *columns*.
    Priority: exact header-unit match > semantic keyword match.
    For 'width'/'depth'/'height', prefers column that also embeds a unit hint.
    """
    mapping: dict[str, str | None] = {f: None for f in _BOQ_COL_KEYWORDS}
    assigned: set[str] = set()

    # First pass: columns that embed a unit keyword get priority for dimension fields
    for field in ("width", "depth", "height"):
        for col in columns:
            if col in assigned:
                continue
            if _boq_detect_unit_from_header(col) and _boq_semantic_match(col, field):
                mapping[field] = col
                assigned.add(col)
                break

    # Second pass: semantic match for remaining fields
    for field in ("label", "width", "depth", "height", "qty", "uom", "xpos", "ypos"):
        if mapping[field] is not None:
            continue
        for col in columns:
            if col in assigned:
                continue
            if _boq_semantic_match(col, field):
                mapping[field] = col
                assigned.add(col)
                break

    return mapping


def _boq_find_header_row(path: "Path", sheet: str, max_scan: int = 12) -> int:
    """
    Scan the first *max_scan* rows of *sheet* to find the row index most likely
    to be a header. Heuristic: row where the most cells contain dimension keywords.
    """
    try:
        raw = pd.read_excel(path, sheet_name=sheet, header=None, nrows=max_scan)
    except Exception:
        return 0

    best_row, best_score = 0, 0
    for ri in range(len(raw)):
        row_vals = [str(v).lower() for v in raw.iloc[ri] if v is not None
                    and not (isinstance(v, float) and pd.isna(v))]
        score = 0
        for cell in row_vals:
            for kw_list in _BOQ_COL_KEYWORDS.values():
                if any(kw in cell for kw in kw_list):
                    score += 1
                    break
        if score > best_score:
            best_score, best_row = score, ri

    return best_row if best_score >= 2 else 0


def _boq_find_best_sheet(xl: "pd.ExcelFile") -> str | None:
    """Return the sheet most likely to contain a fixture BOQ."""
    for name in xl.sheet_names:
        norm = name.lower().strip()
        if any(hint in norm for hint in _BOQ_SHEET_HINTS):
            return name
    # Fallback: first sheet that isn't obviously a cover / notes sheet
    skip = ("cover", "contents", "index", "intro", "note", "general")
    for name in xl.sheet_names:
        if not any(s in name.lower() for s in skip):
            return name
    return xl.sheet_names[0] if xl.sheet_names else None


def _read_project_name_from_metadata_rows(
    path: "Path", sheet: str, header_row: int
) -> str:
    """Read the first meaningful string value above the header row."""
    if header_row == 0:
        return ""
    try:
        raw = pd.read_excel(path, sheet_name=sheet, header=None, nrows=header_row)
        for ri in range(len(raw)):
            for ci in range(min(6, len(raw.columns))):
                val = raw.iloc[ri, ci]
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    continue
                s = str(val).strip()
                if len(s) > 4 and not s.startswith("http"):
                    return s
    except Exception:
        pass
    return ""


def _parse_boq_excel(path: "Path", xl: "pd.ExcelFile") -> dict[str, Any] | None:
    """
    Generic fixture-BOQ Excel parser.

    Works on any workbook where:
      - One sheet (auto-detected) contains a fixture/item schedule
      - Column headers may be anywhere in the first 12 rows
      - Dimension columns may be in mm, ft, m, or in — auto-converted to mm
      - Column names are flexible (matched via semantic keyword groups)

    Returns a parsed dict in the same shape as `_parse_workbook_tables()`,
    or None if no usable fixture data is found.
    """
    boq_sheet = _boq_find_best_sheet(xl)
    if boq_sheet is None:
        return None

    header_row = _boq_find_header_row(path, boq_sheet)

    try:
        df = pd.read_excel(path, sheet_name=boq_sheet, header=header_row)
        df = df.dropna(how="all").reset_index(drop=True)
    except Exception:
        return None

    if df.empty or len(df.columns) < 2:
        return None

    col_map = _boq_map_columns(list(df.columns))

    col_label  = col_map["label"]
    col_width  = col_map["width"]
    col_depth  = col_map["depth"]
    col_height = col_map["height"]
    col_qty    = col_map["qty"]
    col_uom    = col_map["uom"]
    col_xpos   = col_map["xpos"]
    col_ypos   = col_map["ypos"]

    # Must have at least one dimension column to be useful
    if col_width is None and col_depth is None:
        return None

    # Determine per-column units (header hint → value-distribution fallback)
    def _col_unit(col: str | None) -> str:
        if col is None:
            return "mm"
        hdr_unit = _boq_detect_unit_from_header(col)
        if hdr_unit:
            return hdr_unit
        # If there's a UOM column, sample its values
        if col_uom:
            uom_vals = [str(v).strip().lower() for v in df[col_uom].dropna()
                        if str(v).strip().lower() not in ("nan", "")]
            for v in uom_vals:
                u = _normalize_unit(v)
                if u in _TO_MM:
                    return u
        # Value-distribution heuristic
        try:
            numeric_vals = pd.to_numeric(df[col], errors="coerce").dropna().tolist()
            return _boq_auto_unit_from_values([float(v) for v in numeric_vals])
        except Exception:
            return "mm"

    unit_w = _col_unit(col_width)
    unit_d = _col_unit(col_depth)
    unit_h = _col_unit(col_height)

    project_name = _read_project_name_from_metadata_rows(path, boq_sheet, header_row)

    def _to_mm_safe(row: "pd.Series", col: str | None, unit: str) -> float | None:
        if col is None:
            return None
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        try:
            raw_val = float(v)
            if raw_val <= 0:
                return None
            return round(raw_val * _TO_MM.get(unit, 1.0), 2)
        except (TypeError, ValueError):
            return None

    def _to_int_safe(row: "pd.Series", col: str | None, default: int = 1) -> int:
        if col is None:
            return default
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        try:
            return max(1, int(float(v)))
        except (TypeError, ValueError):
            return default

    def _text_safe(row: "pd.Series", col: str | None) -> str:
        if col is None:
            return ""
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip()

    fixtures: list[dict[str, Any]] = []
    cursor_x: float = 0.0
    cursor_y: float = 0.0
    row_max_depth: float = 0.0
    ROW_SPACING = 500.0
    MAX_ROW_WIDTH = 15_000.0

    for _, row in df.iterrows():
        width_mm  = _to_mm_safe(row, col_width,  unit_w)
        depth_mm  = _to_mm_safe(row, col_depth,  unit_d)
        height_mm = _to_mm_safe(row, col_height, unit_h)
        qty       = _to_int_safe(row, col_qty)

        # Accept rows with at least one valid dimension
        if (width_mm is None or width_mm <= 0) and (depth_mm is None or depth_mm <= 0):
            continue

        # Infer missing dimension from the other (square fallback)
        if width_mm and not depth_mm:
            depth_mm = width_mm
        elif depth_mm and not width_mm:
            width_mm = depth_mm

        label_raw = _text_safe(row, col_label)
        label = (label_raw or f"Item {len(fixtures) + 1}")[:_LABEL_MAX_CHARS]

        # Use explicit positions when available; else auto-layout.
        # Without explicit positions, cap expansion to avoid generating thousands
        # of duplicate fixtures from BOQ catalogue qty values (e.g. qty=85 sq.ft).
        xpos = _to_mm_safe(row, col_xpos, "mm")
        ypos = _to_mm_safe(row, col_ypos, "mm")
        has_pos = xpos is not None and ypos is not None
        expand_qty = qty if has_pos else min(qty, 10)

        for i in range(expand_qty):
            if has_pos:
                fx = xpos + i * ((width_mm or 0) + ROW_SPACING)
                fy = ypos
            else:
                if cursor_x > 0 and cursor_x + (width_mm or 0) > MAX_ROW_WIDTH:
                    cursor_y += (row_max_depth or 0) + ROW_SPACING
                    cursor_x = 0.0
                    row_max_depth = 0.0
                fx = cursor_x + (width_mm or 0) / 2
                fy = cursor_y + (depth_mm or 0) / 2
                cursor_x += (width_mm or 0) + ROW_SPACING
                row_max_depth = max(row_max_depth, depth_mm or 0)

            fixtures.append({
                "kind":         (label_raw or "fixture")[:40],
                "x":            fx,
                "y":            fy,
                "label":        label,
                "width_mm":     width_mm,
                "depth_mm":     depth_mm,
                "height_mm":    height_mm,
                "rotation_deg": 0.0,
            })

    if not fixtures:
        return None

    # Bounding room from fixture footprint
    all_x2 = [f["x"] + (f["width_mm"] or 0) / 2 for f in fixtures]
    all_y2 = [f["y"] + (f["depth_mm"] or 0) / 2 for f in fixtures]
    pad = 500.0
    layout_w = (max(all_x2) if all_x2 else 5000.0) + pad
    layout_h = (max(all_y2) if all_y2 else 5000.0) + pad

    rooms: list[dict[str, Any]] = [{
        "name":    project_name or "Layout",
        "area":    round(layout_w * layout_h / 1_000_000, 2),
        "polygon": [[0, 0], [layout_w, 0], [layout_w, layout_h], [0, layout_h]],
    }]

    project_meta: dict[str, Any] = {
        "project_name":  project_name,
        "drawing_title": "FIXTURE LAYOUT",
        "source_format": "boq_excel",
        "boq_sheet":     boq_sheet,
        "header_row":    header_row + 1,
        "dim_units":     {"width": unit_w, "depth": unit_d, "height": unit_h},
        "fixture_count": len(fixtures),
    }

    return {
        "source": "sheet",
        "units": "mm",
        "walls": [],
        "rooms": rooms,
        "fixtures": fixtures,
        "openings": [],
        "tables": [],
        "meta": {
            "sheets":                  xl.sheet_names,
            "svg_dimensions":          [],
            "svg_dimension_offset_mm": 380.0,
            "projectmeta":             project_meta,
            "dimension_callouts":      [],
            "sheet_bundle_format":     "boq_excel",
        },
    }


def parse_excel(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    xl = pd.ExcelFile(p)

    # Unstructured BOQ files (vendor BOQ exports, ad-hoc schedules) have
    # irregular headers and mixed units — detect and parse them first.
    boq_result = _parse_boq_excel(p, xl)
    if boq_result is not None:
        return boq_result

    tables: dict[str, pd.DataFrame] = {}
    labels: dict[str, str] = {}
    name_order: list[str] = []
    tag_order: list[str] = []
    for sheet in xl.sheet_names:
        df = pd.read_excel(p, sheet_name=sheet)
        df = df.dropna(how="all")
        tag = _normalize_sheet_tag(sheet)
        base = tag
        n = 2
        while tag in tables:
            tag = f"{base}_{n}"
            n += 1
        tables[tag] = df
        labels[tag] = sheet.strip()
        name_order.append(sheet.strip())
        tag_order.append(tag)

    out = _parse_workbook_tables(
        tables,
        sheet_labels=labels,
        sheet_order=tag_order,
        format_hint="excel",
    )
    out["meta"]["sheets"] = name_order
    return out


def parse_csv(path: Path | str) -> dict[str, Any]:
    """Load one CSV: either unified multi-table (column sheet/table/…) or single-table by filename stem."""
    p = Path(path)
    df = pd.read_csv(p)
    df = df.dropna(how="all")
    route_col = _detect_sheet_routing_column(df)
    tables: dict[str, pd.DataFrame] = {}
    labels: dict[str, str] = {}
    tag_order: list[str] = []

    if route_col is not None:
        seen_tags: set[str] = set()
        for raw_name, group in df.groupby(route_col, dropna=False):
            if pd.isna(raw_name) or str(raw_name).strip() == "":
                continue
            base_tag = _normalize_sheet_tag(str(raw_name))
            tag = base_tag
            n = 2
            while tag in seen_tags:
                tag = f"{base_tag}_{n}"
                n += 1
            seen_tags.add(tag)
            sub = group.drop(columns=[route_col]).dropna(how="all")
            tables[tag] = sub.reset_index(drop=True)
            labels[tag] = str(raw_name).strip()
            tag_order.append(tag)
        if not tables:
            raise ValueError("CSV has a sheet routing column but no usable rows after grouping.")
        fmt = "csv_unified"
    else:
        tag = _strip_planforge_prefix(_normalize_sheet_tag(p.stem))
        tables[tag] = df
        labels[tag] = p.stem
        tag_order = [tag]
        fmt = "csv_single"

    out = _parse_workbook_tables(
        tables,
        sheet_labels=labels,
        sheet_order=tag_order,
        format_hint=fmt,
    )
    out["meta"]["sheets"] = [labels[t] for t in tag_order]
    return out


def parse_csv_zip_bundle(path: Path | str) -> dict[str, Any]:
    """ZIP archive of *.csv files; filename stem maps to sheet tag (optional planforge_ prefix stripped)."""
    p = Path(path)
    tables: dict[str, pd.DataFrame] = {}
    labels: dict[str, str] = {}
    tag_order: list[str] = []

    with zipfile.ZipFile(p, "r") as zf:
        csv_names = sorted(n for n in zf.namelist() if n.lower().endswith(".csv") and not n.endswith("/"))
        if not csv_names:
            raise ValueError("ZIP bundle contains no .csv files.")
        for name in csv_names:
            stem = Path(name).stem
            base_tag = _strip_planforge_prefix(_normalize_sheet_tag(stem))
            tag = base_tag
            n = 2
            while tag in tables:
                tag = f"{base_tag}_{n}"
                n += 1
            data = zf.read(name)
            dfc = pd.read_csv(io.BytesIO(data))
            dfc = dfc.dropna(how="all")
            tables[tag] = dfc
            labels[tag] = stem
            tag_order.append(tag)

    out = _parse_workbook_tables(
        tables,
        sheet_labels=labels,
        sheet_order=tag_order,
        format_hint="csv_zip_bundle",
    )
    out["meta"]["sheets"] = [labels[t] for t in tag_order]
    return out


def parse_pdf(path: Path | str) -> dict[str, Any]:
    def _is_num(tok: str) -> bool:
        try:
            float(tok)
            return True
        except ValueError:
            return False

    def _first_num_index(tokens: list[str], start: int = 0) -> int | None:
        for i in range(start, len(tokens)):
            if _is_num(tokens[i]):
                return i
        return None

    def _parse_rooms_line(line: str) -> dict[str, Any] | None:
        # Expected (from the PDF sample):
        # R01 RETAIL ROOM 502 46600000 280 0 9810 3740 Main sales floor
        # Sometimes prefixed with a section token, e.g. "Rooms R01 ..."
        toks0 = line.split()
        if len(toks0) < 5:
            return None
        ridx = next((i for i, t in enumerate(toks0) if re.match(r"^R\d+\b", t.upper())), None)
        if ridx is None:
            return None
        toks = toks0[ridx:]
        if len(toks) < 5 or not toks[0].upper().startswith("R"):
            return None
        i_area = _first_num_index(toks, start=1)
        if i_area is None or i_area + 6 >= len(toks):
            return None
        room_id = toks[0]
        room_name = " ".join(toks[1:i_area]).strip()
        # area_sft, area_mm2, origin_x, origin_y, width, depth
        try:
            area_sft = float(toks[i_area])
            origin_x = float(toks[i_area + 2])
            origin_y = float(toks[i_area + 3])
            width_mm = float(toks[i_area + 4])
            depth_mm = float(toks[i_area + 5])
        except ValueError:
            return None
        notes = " ".join(toks[i_area + 6 :]).strip() or None
        return {
            "room_id": room_id,
            "name": room_name,
            "area_sft": area_sft,
            "origin_x_mm": origin_x,
            "origin_y_mm": origin_y,
            "width_mm": width_mm,
            "depth_mm": depth_mm,
            "notes": notes,
        }

    def _parse_walls_line(line: str) -> dict[str, Any] | None:
        # W01 Perimeter-Top 0 0 12090 0 150 WALL_EXT Top boundary
        toks0 = line.split()
        if len(toks0) < 8:
            return None
        widx = next((i for i, t in enumerate(toks0) if re.match(r"^W\d+\b", t.upper())), None)
        if widx is None:
            return None
        toks = toks0[widx:]
        if len(toks) < 8 or not toks[0].upper().startswith("W"):
            return None
        i_x1 = _first_num_index(toks, start=1)
        if i_x1 is None or i_x1 + 5 >= len(toks):
            return None
        wall_id = toks[0]
        label = " ".join(toks[1:i_x1]).strip() or None
        try:
            x1 = float(toks[i_x1])
            y1 = float(toks[i_x1 + 1])
            x2 = float(toks[i_x1 + 2])
            y2 = float(toks[i_x1 + 3])
            thickness = float(toks[i_x1 + 4])
        except ValueError:
            return None
        layer = toks[i_x1 + 5] if i_x1 + 5 < len(toks) else None
        notes = " ".join(toks[i_x1 + 6 :]).strip() or None
        return {
            "wall_id": wall_id,
            "label": label,
            "x1_mm": x1,
            "y1_mm": y1,
            "x2_mm": x2,
            "y2_mm": y2,
            "thickness_mm": thickness,
            "layer": layer,
            "notes": notes,
        }

    def _parse_openings_line(line: str) -> dict[str, Any] | None:
        # O01 entry STORE ENTRY 4800 3740 6000 3740 1200 Main frontage entry
        toks0 = line.split()
        if len(toks0) < 7:
            return None
        oidx = next((i for i, t in enumerate(toks0) if re.match(r"^O\d+\b", t.upper())), None)
        if oidx is None:
            return None
        toks = toks0[oidx:]
        if len(toks) < 7 or not toks[0].upper().startswith("O"):
            return None
        i_x1 = _first_num_index(toks, start=1)
        if i_x1 is None or i_x1 + 4 >= len(toks):
            return None
        opening_id = toks[0]
        opening_type = toks[1] if len(toks) > 1 else "opening"
        label = " ".join(toks[2:i_x1]).strip() or None
        try:
            x1 = float(toks[i_x1])
            y1 = float(toks[i_x1 + 1])
            x2 = float(toks[i_x1 + 2])
            y2 = float(toks[i_x1 + 3])
        except ValueError:
            return None
        width = None
        if i_x1 + 4 < len(toks) and _is_num(toks[i_x1 + 4]):
            width = float(toks[i_x1 + 4])
        notes = " ".join(toks[i_x1 + 5 :]).strip() or None
        return {
            "opening_id": opening_id,
            "type": opening_type,
            "label": label,
            "x1_mm": x1,
            "y1_mm": y1,
            "x2_mm": x2,
            "y2_mm": y2,
            "width_mm": width,
            "notes": notes,
        }

    def _parse_fixtures_line(line: str) -> dict[str, Any] | None:
        # F01 display 65" Smart Signage VW-65-STD 1821 150 1040 600 150 0 REAR_WALL Top-wall display
        toks0 = line.split()
        if len(toks0) < 8:
            return None
        fidx = next((i for i, t in enumerate(toks0) if re.match(r"^F\d+\b", t.upper())), None)
        if fidx is None:
            return None
        toks = toks0[fidx:]
        if len(toks) < 8 or not toks[0].upper().startswith("F"):
            return None
        fixture_id = toks[0]
        kind = toks[1] if len(toks) > 1 else "fixture"
        # Assume first numeric token is cx; token before that is model_ref, everything between is label
        i_cx = _first_num_index(toks, start=2)
        if i_cx is None or i_cx < 4:
            return None
        model_ref = toks[i_cx - 1]
        label = " ".join(toks[2 : i_cx - 1]).strip() or None
        try:
            cx = float(toks[i_cx])
            cy = float(toks[i_cx + 1])
            width = float(toks[i_cx + 2])
            depth = float(toks[i_cx + 3])
            height = float(toks[i_cx + 4])
            rot = float(toks[i_cx + 5])
        except (ValueError, IndexError):
            return None
        zone = toks[i_cx + 6] if i_cx + 6 < len(toks) else None
        notes = " ".join(toks[i_cx + 7 :]).strip() or None
        return {
            "fixture_id": fixture_id,
            "kind": kind,
            "label": label,
            "model_ref": model_ref,
            "cx_mm": cx,
            "cy_mm": cy,
            "width_mm": width,
            "depth_mm": depth,
            "height_mm": height,
            "rotation_deg": rot,
            "zone": zone,
            "notes": notes,
        }

    p = Path(path)
    tables_flat: list[list[list[str | None]]] = []
    text_chunks: list[str] = []

    with pdfplumber.open(p) as pdf:
        for page in pdf.pages:
            text_chunks.append(page.extract_text() or "")
            for table in page.extract_tables() or []:
                tables_flat.append(table)

    rows: list[dict[str, Any]] = []
    for table in tables_flat:
        if not table:
            continue
        header = [str(c or "").lower() for c in table[0]]
        for row in table[1:]:
            if not row:
                continue
            d = {header[i]: row[i] for i in range(min(len(header), len(row)))}
            rows.append(d)

    rooms = []
    for d in rows:
        lower = {str(k).lower(): v for k, v in d.items()}
        name = lower.get("room") or lower.get("name") or lower.get("space")
        if not name:
            continue
        rooms.append(
            {
                "name": str(name),
                "area": None,
                "polygon": [],
                "source_row": d,
            }
        )

    # Fallback for PDFs where table extraction doesn't yield usable rows.
    walls: list[dict[str, Any]] = []
    fixtures: list[dict[str, Any]] = []
    openings: list[dict[str, Any]] = []
    if not rooms:
        all_text = "\n".join(text_chunks)
        for raw in all_text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("--") and "of" in line:
                continue
            if line.lower().startswith("excel sheet:"):
                continue

            r = _parse_rooms_line(line)
            if r:
                rooms.append(
                    {
                        "name": r["name"],
                        "area": r.get("area_sft"),
                        "polygon": _rect_polygon(
                            float(r["width_mm"]),
                            float(r["depth_mm"]),
                            float(r["origin_x_mm"]),
                            float(r["origin_y_mm"]),
                        ),
                        "source_row": r,
                    }
                )
                continue
            w = _parse_walls_line(line)
            if w:
                walls.append({"x1": float(w["x1_mm"]), "y1": float(w["y1_mm"]), "x2": float(w["x2_mm"]), "y2": float(w["y2_mm"])})
                continue
            o = _parse_openings_line(line)
            if o:
                openings.append(
                    {
                        "kind": str(o.get("type") or "opening"),
                        "x1": float(o["x1_mm"]),
                        "y1": float(o["y1_mm"]),
                        "x2": float(o["x2_mm"]),
                        "y2": float(o["y2_mm"]),
                        "label": o.get("label"),
                    }
                )
                continue
            f = _parse_fixtures_line(line)
            if f:
                fixtures.append(
                    {
                        "kind": str(f.get("kind") or "fixture"),
                        "x": float(f["cx_mm"]),
                        "y": float(f["cy_mm"]),
                        "label": (f.get("label") or f.get("fixture_id") or "")[:120] or None,
                        "width_mm": float(f["width_mm"]) if f.get("width_mm") is not None else None,
                        "depth_mm": float(f["depth_mm"]) if f.get("depth_mm") is not None else None,
                        "height_mm": float(f["height_mm"]) if f.get("height_mm") is not None else None,
                        "rotation_deg": float(f["rotation_deg"]) if f.get("rotation_deg") is not None else 0.0,
                    }
                )
                continue

    return {
        "source": "sheet",
        "units": "mm",
        "walls": walls,
        "rooms": rooms,
        "fixtures": fixtures,
        "openings": openings,
        "tables": tables_flat[:20],
        "meta": {"pdf_text_preview": "\n".join(text_chunks)[:4000]},
    }


def parse_sheet(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    suf = p.suffix.lower()
    if suf in (".xlsx", ".xls"):
        return parse_excel(p)
    if suf == ".csv":
        return parse_csv(p)
    if suf == ".zip":
        return parse_csv_zip_bundle(p)
    if suf == ".pdf":
        return parse_pdf(p)
    raise ValueError(f"Unsupported sheet type: {suf}")
