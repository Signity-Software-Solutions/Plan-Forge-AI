"""DXF → structured JSON via ezdxf (lines as walls, TEXT/MTEXT as labels, DIMENSION where available)."""

from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.entities import DXFEntity


def _entity_lines(entity: DXFEntity) -> list[tuple[float, float, float, float]]:
    et = entity.dxftype()
    try:
        if et == "LINE":
            s = entity.dxf.start
            e = entity.dxf.end
            return [(float(s.x), float(s.y), float(e.x), float(e.y))]
        if et == "LWPOLYLINE":
            pts = [(float(x), float(y)) for x, y, *_ in entity.get_points("xy")]
            if len(pts) < 2:
                return []
            out: list[tuple[float, float, float, float]] = []
            for i in range(len(pts) - 1):
                p0, p1 = pts[i], pts[i + 1]
                out.append((p0[0], p0[1], p1[0], p1[1]))
            if entity.closed and len(pts) >= 3:
                p0, p1 = pts[-1], pts[0]
                out.append((p0[0], p0[1], p1[0], p1[1]))
            return out
    except Exception:
        return []
    return []


def _layer_names(doc: Any) -> list[str]:
    """ezdxf LayerTable is iterable but not dict-like (no .keys())."""
    try:
        return [str(layer.dxf.name) for layer in doc.layers]
    except Exception:
        return []


def parse_dxf(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    doc = ezdxf.readfile(p)
    msp = doc.modelspace()

    walls_raw: list[tuple[float, float, float, float]] = []
    labels: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []

    for entity in msp:
        et = entity.dxftype()
        lines = _entity_lines(entity)
        for seg in lines:
            walls_raw.append(seg)

        if et in ("TEXT", "MTEXT"):
            try:
                text = entity.plain_text() if et == "MTEXT" else entity.dxf.text
                insert = entity.dxf.insert
                labels.append(
                    {
                        "text": (text or "").strip(),
                        "x": float(insert.x),
                        "y": float(insert.y),
                    }
                )
            except Exception:
                continue

        if et == "DIMENSION":
            try:
                dim = entity.dxf
                dimensions.append(
                    {
                        "defpoint": getattr(dim, "defpoint", None),
                        "text": getattr(dim, "text", None),
                    }
                )
            except Exception:
                continue

    # Optional: crop to extents for meta
    try:
        extents = bbox.extents(msp)
        bb = {
            "xmin": extents.extmin.x,
            "ymin": extents.extmin.y,
            "xmax": extents.extmax.x,
            "ymax": extents.extmax.y,
        }
    except Exception:
        bb = {}

    return {
        "source": "cad",
        "walls": [{"x1": a, "y1": b, "x2": c, "y2": d} for a, b, c, d in walls_raw],
        "labels": labels,
        "dimensions": dimensions,
        "meta": {"layers": _layer_names(doc), "extents": bb},
    }
