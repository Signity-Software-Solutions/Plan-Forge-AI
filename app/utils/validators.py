"""Validation before generating output — closed polygons, scale, dimensions."""

from dataclasses import dataclass

from shapely.geometry import LinearRing, Polygon as ShapelyPolygon
from shapely.validation import explain_validity

from app.models.layout_schema import UnifiedLayout


@dataclass
class ValidationIssue:
    code: str
    message: str
    detail: str | None = None


def validate_layout(layout: UnifiedLayout) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if layout.units not in ("mm", "m", "ft", "in"):
        issues.append(
            ValidationIssue("units", "Unusual units value; expected mm/m/ft/in.", layout.units)
        )

    for i, room in enumerate(layout.rooms):
        poly = room.polygon
        if len(poly) < 3:
            issues.append(
                ValidationIssue(
                    "room_polygon",
                    f"Room '{room.name or i}' needs at least 3 vertices.",
                    str(len(poly)),
                )
            )
            continue
        try:
            ring = LinearRing([(float(p[0]), float(p[1])) for p in poly])
            if not ring.is_closed:
                issues.append(
                    ValidationIssue(
                        "room_not_closed",
                        f"Room '{room.name or i}' ring not closed.",
                        explain_validity(ring),
                    )
                )
            sh_poly = ShapelyPolygon(ring)
            if not sh_poly.is_valid:
                issues.append(
                    ValidationIssue(
                        "invalid_polygon",
                        f"Room '{room.name or i}' invalid geometry.",
                        explain_validity(sh_poly),
                    )
                )
            if room.area is not None and sh_poly.is_valid:
                # crude consistency check (unit-agnostic ratio sanity)
                computed = abs(sh_poly.area)
                if computed > 0 and room.area > 0:
                    ratio = max(computed, room.area) / max(min(computed, room.area), 1e-9)
                    if ratio > 2.0:
                        issues.append(
                            ValidationIssue(
                                "area_mismatch",
                                f"Room '{room.name or i}' labeled area vs polygon area differ strongly.",
                                f"labeled={room.area}, geom≈{computed}",
                            )
                        )
        except Exception as e:
            issues.append(
                ValidationIssue(
                    "room_geometry",
                    f"Room '{room.name or i}' could not be validated.",
                    str(e),
                )
            )

    # Wall segments: warn on zero-length
    for j, w in enumerate(layout.walls):
        dx, dy = w.x2 - w.x1, w.y2 - w.y1
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            issues.append(
                ValidationIssue("degenerate_wall", f"Wall segment {j} has zero length.", None)
            )

    return issues


def validation_errors_only(layout: UnifiedLayout) -> list[ValidationIssue]:
    return [i for i in validate_layout(layout) if i.code in ("room_polygon", "room_not_closed", "invalid_polygon")]
