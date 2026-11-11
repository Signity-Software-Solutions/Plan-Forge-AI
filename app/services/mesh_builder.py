"""
3D massing (Tier A) — UnifiedLayout → GLB via trimesh.

Peer to ``geometry_engine`` (SVG) and ``dxf_exporter`` (DXF): consumes the same
``UnifiedLayout`` and changes nothing upstream of it.

Scope is deliberately plain boxes:

  * walls      extruded rectangles (length x thickness x height)
  * openings   sub-rectangle decomposition (jamb / lintel / sill boxes) rather
               than boolean subtraction, so results are watertight by
               construction and an opening flush to a wall end degrades to a
               zero-width jamb instead of a degenerate boolean operand
  * fixtures   one box per fixture from its 2D footprint + a height
  * ceiling    room polygons extruded as thin slabs (earcut triangulation)

Coordinate system
    Layout X/Y are copied verbatim; only Z is added. The scene is **Z-up in
    layout units (mm)**, matching the plan and the DXF exporter, which is what
    makes the "3D bbox equals 2D bounds" invariant literally true. glTF is a
    Y-up format, so the group node ``PLANFORGE_ROOT`` carries the Z-up→Y-up
    rotation and viewers get a correctly oriented model without the geometry
    itself being rewritten.

Triangulation
    ``engine="earcut"`` is passed explicitly at every ``extrude_polygon`` call.
    trimesh would otherwise be free to select the ``triangle`` package, which
    is not licensed for commercial use.

Z provenance
    ``UnifiedLayout`` carries no ceiling height and no wall thickness. Values
    are resolved per layout from ``meta`` first, then settings defaults; the
    result is reported in ``MassingResult.provenance`` with ``z_source`` set to
    ``"user_supplied"`` or ``"assumed_default"``. An assumed ceiling height is
    an unbounded, undetectable error, so callers are expected to surface it.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image
from shapely.geometry import Polygon as SPolygon

from app.core.config import get_settings
from app.models.layout_schema import Fixture, Opening, UnifiedLayout, WallSegment
from app.services import asset_loader, fixture_types as ftypes
from app.services import visual_profiles as vprofiles
from app.services.resolution import (
    SOURCE_GLOBAL_DEFAULT,
    SOURCE_PARSED,
    SOURCE_TYPE_DEFAULT,
    SOURCE_USER,
    Resolution,
    ResolutionLog,
    meta_lookup,
    non_negative_float,
    positive_float,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Scene groups. The frontend toggles visibility per group, so these names are
# part of the GLB contract — do not rename without updating the viewer.
GROUP_ROOT = "PLANFORGE_ROOT"
GROUP_WALLS = "walls"
GROUP_FIXTURES = "fixtures"
GROUP_CEILING = "ceiling"
GROUP_FLOOR = "floor"

GROUPS = (GROUP_WALLS, GROUP_FIXTURES, GROUP_CEILING, GROUP_FLOOR)

# Flat neutral shades, company-neutral by design. Enough for the shapes to read
# apart from each other; not a brand palette and not texture work.
#
# Floor and cabinetry are measured, not guessed: sampling the dominant colour
# clusters of a real retail design proposal's plan view gives an oak floor
# around (0.75, 0.66, 0.56) and cabinetry at (0.09, 0.09, 0.09) — together about
# a quarter of all model pixels. The earlier hand-picked floor was noticeably too
# orange. The greys are still judgement; sampling could not separate interior
# wall from shadowed wall reliably.
_PALETTE: dict[str, tuple[float, float, float, float]] = {
    "wall": (0.82, 0.82, 0.81, 1.0),       # pale concrete perimeter
    # Reference proposal's shopfront frame reads near-black -- close to the same
    # measured cabinetry tone, not a separately sampled colour of its own.
    "wall_shopfront": (0.10, 0.10, 0.11, 1.0),
    "ceiling": (0.90, 0.90, 0.90, 1.0),
    "floor": (0.75, 0.66, 0.56, 1.0),      # warm oak plank (measured)
    ftypes.MAT_BODY: (0.09, 0.09, 0.09, 1.0),   # near-black cabinetry (measured)
    ftypes.MAT_TOP: (0.96, 0.96, 0.96, 1.0),    # white worktop
    ftypes.MAT_PANEL: (0.86, 0.86, 0.85, 1.0),  # light backpanel / product zone
    ftypes.MAT_BOXING: (0.20, 0.21, 0.22, 1.0), # dark upper boxing band
    ftypes.MAT_SCREEN: (0.05, 0.05, 0.07, 1.0),
    ftypes.MAT_ACCENT: (0.42, 0.46, 0.55, 1.0),
    ftypes.MAT_GENERIC: (0.62, 0.62, 0.62, 1.0),
    # Backlit panel face. baseColorFactor stays a plain cool white — the glow
    # comes entirely from emissiveFactor below, not from any texture.
    ftypes.MAT_EMISSIVE: (0.85, 0.90, 0.95, 1.0),
}
_COLOUR_FALLBACK = (0.55, 0.57, 0.60, 1.0)

# Materials that glow rather than merely reflect. Kept separate from _PALETTE's
# baseColorFactor so "flat neutral shade" still describes every other material —
# this is the one deliberate exception, for fixtures that are lit in reality.
_EMISSIVE: dict[str, tuple[float, float, float]] = {
    ftypes.MAT_EMISSIVE: (0.65, 0.80, 1.00),
}

# How rough each surface reads. Previously every material shipped at a blanket
# 1.0 — fully matte, no specular response at all — which is why every surface
# read as chalk regardless of what it was meant to be. These are judgement
# calls against the reference proposal, not measurements: a lacquered worktop
# and a lit screen are visibly glossier than a cast-concrete wall, and saying so
# costs nothing in file size.
_ROUGHNESS: dict[str, float] = {
    "wall": 0.92,
    "wall_shopfront": 0.70,
    "ceiling": 0.95,
    "floor": 0.55,               # sealed timber, the one clearly reflective plane
    ftypes.MAT_BODY: 0.55,       # matte laminate cabinetry
    ftypes.MAT_TOP: 0.35,        # lacquered worktop
    ftypes.MAT_PANEL: 0.80,
    ftypes.MAT_BOXING: 0.75,
    ftypes.MAT_SCREEN: 0.15,     # glass
    ftypes.MAT_ACCENT: 0.70,
    ftypes.MAT_GENERIC: 0.85,
    ftypes.MAT_EMISSIVE: 0.60,
}
_ROUGHNESS_FALLBACK = 0.85

# Tileable CC0 textures, by material key: (file, tile width mm, tile height mm).
# Only surfaces large enough for a repeat to read are textured; a 100mm-deep
# fixture band shows too little of a tile to be worth the payload. See
# ../assets/textures/manifest.json for provenance and for which candidates were
# deliberately not shipped.
# Where the entrance-facing plan camera sits. 40 degrees is the look -- shallow
# enough to read as a room rather than a map, and roughly where the reference
# proposal puts its own plan view -- but it cannot be the whole rule, because
# wall height is absolute while room size is not: the same angle that hides 19%
# of a 13m shop's floor behind its entrance wall hides half of a 6m one. So the
# camera rises above 40 degrees whenever that is what it takes to keep the
# occluded slice within _PLAN_MAX_OCCLUSION of the plan's depth.
_PLAN_ELEVATION_DEG = 40.0
_PLAN_MAX_OCCLUSION = 0.22

_TEXTURE_DIR = Path(__file__).resolve().parent.parent / "assets" / "textures"


@dataclass(frozen=True)
class _TextureSpec:
    """One tileable material's full map set. ``orm`` and ``normal`` are optional —
    a material can ship colour only, same as before this pair was added."""

    color: str
    tile_w: float
    tile_h: float
    # Packed R=occlusion, G=roughness, B=metalness — the glTF spec's own
    # suggested optimisation, one image doing the job of two texture slots
    # (metallicRoughnessTexture and occlusionTexture) rather than shipping
    # near-duplicate JPEGs for each.
    orm: str | None = None
    # NormalGL (OpenGL/+Y) convention, matching glTF's own tangent-space rule.
    normal: str | None = None


_TEXTURED: dict[str, _TextureSpec] = {
    "floor": _TextureSpec("oak_floor.jpg", 1200.0, 1200.0, orm="oak_floor_orm.jpg", normal="oak_floor_normal.jpg"),
    "wall": _TextureSpec(
        "concrete_wall.jpg", 2400.0, 1200.0, orm="concrete_wall_orm.jpg", normal="concrete_wall_normal.jpg"
    ),
}

# Opening elevations by kind (mm above floor), from the retail-plan defaults in
# the 3D brief. Head of "opening" is the ceiling, resolved per layout.
_OPENING_ELEVATIONS: dict[str, tuple[float, float | None]] = {
    "door": (0.0, 2100.0),
    "window": (900.0, 2400.0),
    "opening": (0.0, None),
}
_OPENING_FALLBACK = "door"

# An opening is bound to a wall when its midpoint lies within this distance of
# the wall centreline (mm). Openings come from parsers as absolute segments, so
# they rarely sit exactly on the centreline.
_OPENING_SNAP_TOL_MM = 600.0

# Geometry below this size (mm) is dropped rather than emitted as a degenerate
# box — this is the zero-width jamb case.
_EPS_MM = 1e-6

# Shadow-gap between vertically stacked bands (mm), shaved off the top of
# every band before it is built. Real casework never has two panels meeting
# in a mathematically perfect seam -- there is always a reveal, and that
# reveal is what tells a viewer "this is an assembly of discrete parts," not
# one extruded slab with a colour change partway up. Clamped per-band to a
# fraction of its own height (see the loop below) so a thin shelf or handle
# groove is not shaved down to nothing.
_BAND_REVEAL_MM = 3.0

# Wall-bay run merging. Adjacent fixtures of these types, on the same wall line,
# with no more than this gap between their edges, render as a visually
# continuous run rather than separate boxes with a seam between them — matching
# how the reference proposal's shelving reads as unbroken runs down each wall.
# 150mm is small relative to a ~1100mm bay (a residual placement gap, not a real
# architectural break) and well under the opening-snap tolerance above.
_MERGEABLE_FIXTURE_TYPES = frozenset({"wall_bay", "base_cabinet"})
_RUN_GAP_TOLERANCE_MM = 150.0
# Fixtures on the same wall line within this perpendicular offset are treated
# as the same row. Real BOQ/plan data rarely places same-run units perfectly
# collinear, so this is deliberately looser than the gap tolerance above.
_RUN_ROW_TOLERANCE_MM = 40.0

_CEILING_SLAB_THICKNESS_MM = 50.0
_FLOOR_SLAB_THICKNESS_MM = 50.0


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class EmptyMassingError(ValueError):
    """Raised when a layout contains nothing that can be extruded."""


@dataclass
class MassingResult:
    """A built massing scene plus the metadata the viewer needs."""

    scene: trimesh.Scene
    provenance: dict[str, Any] = field(default_factory=dict)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    viewpoints: list[dict[str, Any]] = field(default_factory=list)
    resolutions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.scene.geometry

    def export_glb(self) -> bytes:
        """
        Serialize to GLB bytes. Never writes to disk — see routes_output.

        Raises ``EmptyMassingError`` when the layout produced no geometry:
        trimesh refuses to export an empty scene, and a caller needs to tell
        "nothing to extrude" apart from a real export failure.
        """
        if self.is_empty:
            raise EmptyMassingError(
                "Layout produced no massing geometry "
                "(no walls with length, no fixtures with a footprint, no room polygons)."
            )
        return self.scene.export(file_type="glb")

    def vertex_hash(self) -> str:
        """Stable digest of all geometry, for determinism tests and ETags."""
        h = hashlib.sha256()
        for name in sorted(self.scene.geometry):
            mesh = self.scene.geometry[name]
            h.update(name.encode("utf-8"))
            h.update(np.ascontiguousarray(mesh.vertices, dtype=np.float64).tobytes())
            h.update(np.ascontiguousarray(mesh.faces, dtype=np.int64).tobytes())
        return h.hexdigest()


# ---------------------------------------------------------------------------
# Z resolution
# ---------------------------------------------------------------------------


def resolve_ceiling_height_mm(
    layout: UnifiedLayout, log: ResolutionLog | None = None
) -> tuple[float, str]:
    """
    Ceiling height, resolved through the ladder.

    R-CEIL-01  meta.ceiling_height_mm   user_supplied
    R-CEIL-05  config default           global_default
    """
    value = positive_float((layout.meta or {}).get("ceiling_height_mm"))
    if value is not None:
        res = Resolution("ceiling_height_mm", value, SOURCE_USER, "R-CEIL-01")
    else:
        res = Resolution(
            "ceiling_height_mm",
            float(get_settings().mesh_ceiling_height_mm),
            SOURCE_GLOBAL_DEFAULT,
            "R-CEIL-05",
        )
    if log is not None:
        log.record(res)
    return res.value, res.source


def resolve_wall_thickness_mm(
    layout: UnifiedLayout, log: ResolutionLog | None = None
) -> tuple[float, str]:
    """
    Wall thickness. ``WallSegment`` is a bare centreline, so this is always
    either declared or assumed — never measured.

    R-WALL-01  meta.wall_thickness_mm   user_supplied
    R-WALL-05  config default           global_default
    """
    value = positive_float((layout.meta or {}).get("wall_thickness_mm"))
    if value is not None:
        res = Resolution("wall_thickness_mm", value, SOURCE_USER, "R-WALL-01")
    else:
        res = Resolution(
            "wall_thickness_mm",
            float(get_settings().mesh_wall_thickness_mm),
            SOURCE_GLOBAL_DEFAULT,
            "R-WALL-05",
        )
    if log is not None:
        log.record(res)
    return res.value, res.source


def resolve_fixture_height_mm(
    f: Fixture, type_key: str, log: ResolutionLog | None = None, subject: str = ""
) -> tuple[float, str]:
    """
    R-FXH-02  height_mm present in the layout      parsed
    R-FXH-04  the fixture type's default height    type_default
    """
    value = positive_float(f.height_mm)
    if value is not None:
        res = Resolution("height_mm", value, SOURCE_PARSED, "R-FXH-02", subject)
    else:
        res = Resolution(
            "height_mm",
            ftypes.get_type(type_key).default_h,
            SOURCE_TYPE_DEFAULT,
            "R-FXH-04",
            subject,
        )
    if log is not None:
        log.record(res)
    return res.value, res.source


def resolve_fixture_elevation_mm(
    f: Fixture,
    layout: UnifiedLayout,
    type_key: str = ftypes.FALLBACK_TYPE,
    log: ResolutionLog | None = None,
    subject: str = "",
) -> tuple[float, str]:
    """
    Height of a fixture's underside above the floor.

    ``Fixture`` has no elevation field, so everything would sit on the floor.
    That is wrong for wall-hung displays, whose ``height_mm`` is the panel
    thickness rather than a floor-to-top dimension — left alone, a wall-mounted
    screen renders lying on the floor.

    R-FXE-01  meta.fixture_elevations_mm[label|kind|"*"]   user_supplied
    R-FXE-04  the fixture type's default elevation         type_default
    """
    raw = meta_lookup(
        layout.meta, "fixture_elevations_mm", f.label or "", f.kind or "", "*"
    )
    value = non_negative_float(raw)
    if value is not None:
        res = Resolution("elevation_mm", value, SOURCE_USER, "R-FXE-01", subject)
    else:
        if raw is not None:
            logger.warning("fixture_elevations_mm entry not numeric (%r); ignored", raw)
        res = Resolution(
            "elevation_mm",
            ftypes.get_type(type_key).default_elevation,
            SOURCE_TYPE_DEFAULT,
            "R-FXE-04",
            subject,
        )
    if log is not None:
        log.record(res)
    return res.value, res.source


def _opening_elevations_mm(o: Opening, ceiling_mm: float) -> tuple[float, float]:
    """(sill, head) in mm above floor, clamped into [0, ceiling]."""
    kind = (o.kind or "").lower()
    sill, head = _OPENING_ELEVATIONS.get(kind, _OPENING_ELEVATIONS[_OPENING_FALLBACK])
    head_mm = ceiling_mm if head is None else float(head)
    sill_mm = max(0.0, min(float(sill), ceiling_mm))
    head_mm = max(sill_mm, min(head_mm, ceiling_mm))
    return sill_mm, head_mm


# ---------------------------------------------------------------------------
# Wall / opening geometry
# ---------------------------------------------------------------------------


def _wall_length(w: WallSegment) -> float:
    return math.hypot(w.x2 - w.x1, w.y2 - w.y1)


def _box(
    length: float,
    width: float,
    height: float,
    *,
    centre: tuple[float, float, float],
    yaw_rad: float,
) -> trimesh.Trimesh | None:
    """
    Axis-aligned box of (length, width, height) rotated ``yaw_rad`` about Z and
    moved to ``centre``. Returns None for degenerate input — this is the
    zero-width jamb path, which must not raise.
    """
    if length <= _EPS_MM or width <= _EPS_MM or height <= _EPS_MM:
        return None
    mesh = trimesh.creation.box(extents=(length, width, height))
    transform = trimesh.transformations.rotation_matrix(yaw_rad, (0.0, 0.0, 1.0))
    transform[:3, 3] = centre
    mesh.apply_transform(transform)
    return mesh


def _rounded_box(
    length: float,
    width: float,
    height: float,
    *,
    centre: tuple[float, float, float],
    yaw_rad: float,
    radius: float,
) -> trimesh.Trimesh | None:
    """
    ``_box`` with radiused vertical corners.

    The radius is clamped to just under half the shorter side, so an aggressive
    recipe on a nearly-square band degrades to a stadium shape rather than
    collapsing or inverting the polygon. Falls back to a plain box for a radius
    too small to be worth the extra geometry.

    The footprint is inscribed in the same rectangle ``_box`` would produce, so
    the bounding box is identical and the X/Y bounds guarantee is untouched.
    """
    if length <= _EPS_MM or width <= _EPS_MM or height <= _EPS_MM:
        return None
    r = min(radius, min(length, width) / 2.0 - _EPS_MM)
    if r <= 1.0:
        return _box(length, width, height, centre=centre, yaw_rad=yaw_rad)

    core = SPolygon(
        [
            (-length / 2.0 + r, -width / 2.0 + r),
            (length / 2.0 - r, -width / 2.0 + r),
            (length / 2.0 - r, width / 2.0 - r),
            (-length / 2.0 + r, width / 2.0 - r),
        ]
    )
    mesh = trimesh.creation.extrude_polygon(core.buffer(r, quad_segs=4), height)
    # extrude_polygon builds from z=0 up; _box is centred, and every caller
    # below assumes a centred primitive.
    mesh.apply_translation((0.0, 0.0, -height / 2.0))
    transform = trimesh.transformations.rotation_matrix(yaw_rad, (0.0, 0.0, 1.0))
    transform[:3, 3] = centre
    mesh.apply_transform(transform)
    return mesh


def _bevelled_top_box(
    length: float,
    width: float,
    height: float,
    *,
    centre: tuple[float, float, float],
    yaw_rad: float,
    bevel: float,
) -> trimesh.Trimesh | None:
    """
    ``_box`` with the top face inset ``bevel`` mm on every side, so the band
    renders as a truncated pyramid (a chamfered top edge) rather than a
    hard-edged slab — the same "untreated massing" tell ``_rounded_box`` fixes
    for corners, but for a worktop or boxing lip's top edge instead.

    Explicit frustum mesh: 8 vertices (a full-size bottom rectangle, a
    ``bevel``-inset top rectangle) and 12 hand-wound triangles. This is a
    fixed, known topology, so it is built directly rather than through
    ``convex_hull`` — trimesh's hull routine needs scipy, which is not a
    dependency of this project, and a rectangular frustum's faces do not need
    a general-purpose hull algorithm to get right.

    The bottom face is unchanged, so the footprint (and the X/Y bounds
    guarantee) matches ``_box`` exactly; only the top face shrinks.
    """
    if length <= _EPS_MM or width <= _EPS_MM or height <= _EPS_MM:
        return None
    b = max(0.0, min(bevel, length / 2.0 - _EPS_MM, width / 2.0 - _EPS_MM, height - _EPS_MM))
    if b <= 1.0:
        return _box(length, width, height, centre=centre, yaw_rad=yaw_rad)

    hl, hw, hh = length / 2.0, width / 2.0, height / 2.0
    # 0-3 bottom (full size), 4-7 top (inset by b), both wound CCW as seen
    # from +Z looking down, matching how the side-face winding below was
    # derived (checked by hand: every face's vertex order gives an outward
    # cross product, not by trusting trimesh to repair it after the fact).
    verts = np.array(
        [
            (-hl, -hw, -hh), (hl, -hw, -hh), (hl, hw, -hh), (-hl, hw, -hh),
            (-hl + b, -hw + b, hh), (hl - b, -hw + b, hh),
            (hl - b, hw - b, hh), (-hl + b, hw - b, hh),
        ]
    )
    faces = np.array(
        [
            [0, 3, 2], [0, 2, 1],  # bottom, normal -Z
            [4, 5, 6], [4, 6, 7],  # top, normal +Z
            [0, 1, 5], [0, 5, 4],  # front, normal -Y
            [1, 2, 6], [1, 6, 5],  # right, normal +X
            [2, 3, 7], [2, 7, 6],  # back, normal +Y
            [3, 0, 4], [3, 4, 7],  # left, normal -X
        ]
    )
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    transform = trimesh.transformations.rotation_matrix(yaw_rad, (0.0, 0.0, 1.0))
    transform[:3, 3] = centre
    mesh.apply_transform(transform)
    return mesh


def _place_prop_in_cell(
    asset_key: str,
    *,
    cell_w: float,
    cell_d: float,
    lay_flat: bool,
    tilt_deg: float = 0.0,
    target_size_mm: float,
    centre: tuple[float, float, float],
    yaw_rad: float,
) -> trimesh.Trimesh | None:
    """
    One repeat-grid cell's worth of a shared product-prop asset (see
    ``asset_loader``), scaled to ``target_size_mm`` and clamped to fit inside
    ``cell_w`` x ``cell_d`` -- same "shrink to fit, never distort past the
    footprint" discipline every other allowance in the band loop follows,
    applied to a placed mesh instead of a procedural box.

    ``lay_flat`` tips the asset from its authored standing orientation onto
    its back (rotate -90 degrees about X), for product resting on a table
    rather than standing on a shelf. ``tilt_deg`` (ignored when ``lay_flat``
    is set) reclines it partway there instead -- see ``Band.asset_tilt_deg``
    for why a partial recline matters and a boolean flat/standing choice
    isn't enough on its own.

    Returns ``None`` if the asset can't be loaded, so the caller falls back
    to a procedural band -- ``asset_loader.load_asset`` already treats a
    missing/broken asset as a first-class outcome, not an error, and this
    preserves that all the way up.
    """
    base = asset_loader.load_asset(asset_key)
    if base is None:
        return None
    # asset_loader caches and returns the identical mesh object per key, so
    # this material reference is the same Python object across every one of
    # this asset's placements -- capturing it before .copy() (which deep-
    # copies visual/material, unlike geometry-only reuse) and reassigning it
    # below is what lets the exporter emit the texture once, not once per
    # placed copy. A shelf full of these otherwise embeds the same 20KB
    # texture a dozen-plus times over.
    #
    # Only a TextureVisuals asset needs this dedup at all: its material holds
    # an embedded image, the actual GLB-bloat risk. A ColorVisuals asset (a
    # flat-shaded prop with no texture at all -- same convention chair.glb
    # and stool.glb already ship under, see asset_loader) carries only a
    # small per-vertex colour array; .copy() duplicating that costs nothing
    # worth sharing a Python object over, so it is simply left alone.
    is_textured = isinstance(base.visual, trimesh.visual.TextureVisuals)
    shared_material = base.visual.material if is_textured else None
    mesh = base.copy()

    # A band's asset_lay_flat request only applies to an asset normalised
    # standing (see asset_loader.native_pose) -- one already normalised
    # resting in its own natural table pose (a laptop: physically no thin
    # axis to stand on) would be tipped into a wrong orientation by the same
    # transform that correctly lays a standing phone onto its back. This is
    # what lets a single catalog (Band.asset_key as a tuple) mix both kinds
    # of asset under one asset_lay_flat=True band.
    if asset_loader.native_pose(asset_key) == "standing":
        if lay_flat:
            mesh.apply_transform(
                trimesh.transformations.rotation_matrix(-math.pi / 2.0, (1.0, 0.0, 0.0))
            )
        elif tilt_deg:
            mesh.apply_transform(
                trimesh.transformations.rotation_matrix(-math.radians(tilt_deg), (1.0, 0.0, 0.0))
            )

    lo, hi = mesh.bounds
    extent = hi - lo
    native_size = float(max(extent))
    if native_size <= _EPS_MM:
        return None
    scale = target_size_mm / native_size

    # Clamp so the target size can never push the prop past the cell it was
    # given -- a placement choice, not a measurement, so it must yield to the
    # footprint the same way project_d and recess_mm already do.
    scaled_w, scaled_d = extent[0] * scale, extent[1] * scale
    overflow = max(
        scaled_w / cell_w if cell_w > _EPS_MM else 1.0,
        scaled_d / cell_d if cell_d > _EPS_MM else 1.0,
        1.0,
    )
    scale /= overflow

    mesh.apply_scale(scale)
    lo, hi = mesh.bounds
    # Re-centre in X/Y and sit the base at z=0 (local), matching
    # asset_loader._normalise's own footprint-centred / floor-at-zero
    # convention so the placement transform below is a plain rotate+move.
    mesh.apply_translation((-(lo[0] + hi[0]) / 2.0, -(lo[1] + hi[1]) / 2.0, -lo[2]))

    transform = trimesh.transformations.rotation_matrix(yaw_rad, (0.0, 0.0, 1.0))
    transform[:3, 3] = centre
    mesh.apply_transform(transform)

    if is_textured:
        # Restore the shared material/texture object .copy() deep-copied away.
        mesh.visual = trimesh.visual.TextureVisuals(uv=mesh.visual.uv, material=shared_material)
    # else: ColorVisuals survives .copy() and every transform above untouched
    # -- nothing to restore.
    return mesh


def _bind_openings_to_walls(
    walls: list[WallSegment],
    openings: list[Opening],
) -> dict[int, list[tuple[float, float, Opening]]]:
    """
    Group openings by the wall they sit on.

    ``Opening`` is an absolute segment (x1,y1,x2,y2) rather than the
    wall-relative ``{wall_idx, offset, width}`` the 3D brief assumed, so the
    association is recovered geometrically: the wall whose centreline is
    nearest the opening midpoint, within ``_OPENING_SNAP_TOL_MM``.

    Returns ``{wall_index: [(along_start, along_end, opening), ...]}`` with
    spans measured from the wall's (x1,y1) end and sorted along the wall.
    """
    bound: dict[int, list[tuple[float, float, Opening]]] = {}

    for o in openings:
        mx, my = (o.x1 + o.x2) / 2.0, (o.y1 + o.y2) / 2.0
        best_idx: int | None = None
        best_dist = float("inf")
        best_span: tuple[float, float] = (0.0, 0.0)

        for idx, w in enumerate(walls):
            length = _wall_length(w)
            if length <= _EPS_MM:
                continue
            ux, uy = (w.x2 - w.x1) / length, (w.y2 - w.y1) / length

            # Perpendicular distance from the opening midpoint to the segment.
            t_mid = (mx - w.x1) * ux + (my - w.y1) * uy
            t_clamped = max(0.0, min(t_mid, length))
            px, py = w.x1 + ux * t_clamped, w.y1 + uy * t_clamped
            dist = math.hypot(mx - px, my - py)
            if dist >= best_dist:
                continue

            # Project both endpoints to get the span along the wall.
            t1 = (o.x1 - w.x1) * ux + (o.y1 - w.y1) * uy
            t2 = (o.x2 - w.x1) * ux + (o.y2 - w.y1) * uy
            s0, s1 = (t1, t2) if t1 <= t2 else (t2, t1)
            # Clamp into the wall; an opening flush to (or past) an end yields a
            # zero-length solid segment there, i.e. a zero-width jamb.
            s0, s1 = max(0.0, min(s0, length)), max(0.0, min(s1, length))
            if s1 - s0 <= _EPS_MM:
                continue

            best_idx, best_dist, best_span = idx, dist, (s0, s1)

        if best_idx is None or best_dist > _OPENING_SNAP_TOL_MM:
            logger.debug(
                "opening %r not bound to any wall (nearest %.1fmm); skipped",
                o.label or o.kind,
                best_dist,
            )
            continue
        bound.setdefault(best_idx, []).append((best_span[0], best_span[1], o))

    for idx in bound:
        bound[idx].sort(key=lambda t: (t[0], t[1]))
    return bound


def _merge_spans(
    spans: list[tuple[float, float, Opening]],
) -> list[tuple[float, float, Opening]]:
    """Drop overlaps so solid segments never come out negative-length."""
    out: list[tuple[float, float, Opening]] = []
    for s0, s1, o in spans:
        if out and s0 < out[-1][1] - _EPS_MM:
            # Overlapping openings on one wall: keep the first, skip the rest.
            logger.debug("overlapping opening %r skipped", o.label or o.kind)
            continue
        out.append((s0, s1, o))
    return out


def _identify_shopfront_walls(
    layout: UnifiedLayout, bound: dict[int, list[tuple[float, float, Opening]]]
) -> set[int]:
    """
    Which wall indices carry the shopfront — the entrance-facing wall(s) that
    render in the reference proposal's dark charcoal, everywhere else staying
    the pale perimeter tone.

    ``UnifiedLayout`` has no entrance concept at all: no field on ``Opening`` or
    anywhere else marks one opening as *the* entrance. So this is read off
    whatever the data actually carries, in order:

      1. Any opening whose label names it as an entrance (case-insensitive
         substring match on "entrance" or "shopfront") — the strongest signal
         when a source happens to supply it.
      2. Otherwise, every "door"-kind opening — the only structural proxy the
         schema has for "this is where people walk in." In a plan with several
         doors this over-marks rather than guessing which one is primary, which
         is the safer failure mode for a colour choice.

    Returns an empty set — not a guess — when neither signal is present, e.g. a
    layout with only windows or no openings at all.
    """
    labelled: set[int] = set()
    doors: set[int] = set()
    for wall_index, spans in bound.items():
        for _, _, opening in spans:
            label = (opening.label or "").lower()
            if "entrance" in label or "shopfront" in label:
                labelled.add(wall_index)
            if (opening.kind or "").lower() == "door":
                doors.add(wall_index)
    return labelled or doors


def _build_wall_meshes(
    w: WallSegment,
    spans: list[tuple[float, float, Opening]],
    *,
    thickness_mm: float,
    ceiling_mm: float,
) -> list[trimesh.Trimesh]:
    """
    One wall as jamb / lintel / sill boxes around its openings.

    A wall with no openings is a single box. Every sub-box is a separate
    watertight solid; none is produced by subtraction.
    """
    length = _wall_length(w)
    if length <= _EPS_MM or ceiling_mm <= _EPS_MM:
        return []

    yaw = math.atan2(w.y2 - w.y1, w.x2 - w.x1)
    ux, uy = math.cos(yaw), math.sin(yaw)

    def place(s_start: float, s_end: float, z0: float, z1: float) -> trimesh.Trimesh | None:
        seg_len = s_end - s_start
        h = z1 - z0
        s_mid = (s_start + s_end) / 2.0
        return _box(
            seg_len,
            thickness_mm,
            h,
            centre=(w.x1 + ux * s_mid, w.y1 + uy * s_mid, z0 + h / 2.0),
            yaw_rad=yaw,
        )

    meshes: list[trimesh.Trimesh] = []
    cursor = 0.0

    for s0, s1, o in _merge_spans(spans):
        sill_mm, head_mm = _opening_elevations_mm(o, ceiling_mm)
        # Full-height jamb between the previous opening and this one. Zero-length
        # when the opening is flush with the wall end — `place` returns None.
        jamb = place(cursor, s0, 0.0, ceiling_mm)
        if jamb is not None:
            meshes.append(jamb)
        if sill_mm > _EPS_MM:  # windows only
            sill = place(s0, s1, 0.0, sill_mm)
            if sill is not None:
                meshes.append(sill)
        if ceiling_mm - head_mm > _EPS_MM:
            lintel = place(s0, s1, head_mm, ceiling_mm)
            if lintel is not None:
                meshes.append(lintel)
        cursor = s1

    tail = place(cursor, length, 0.0, ceiling_mm)
    if tail is not None:
        meshes.append(tail)
    return meshes


def _group_wall_bay_runs(
    layout: UnifiedLayout,
) -> dict[int, tuple[float, float, int, int, bool, bool]]:
    """
    Find contiguous runs of same-type wall-bay/base-cabinet fixtures and compute,
    per fixture index, how much to extend its rendered edges to close the gap to
    its immediate neighbours in the run.

    Returns ``{fixture_index: (extend_neg_mm, extend_pos_mm, run_id, run_size,
    is_run_start, is_run_end)}`` for fixtures that are part of a run of 2 or
    more; fixtures not in the dict render unchanged (a run of 1 is not a run).

    Deliberately uses only declared, unresolved data (``f.height_mm`` as stated,
    not the fully resolved value) — grouping runs off the resolved value would
    mean calling the height/elevation resolvers a second time, writing duplicate
    entries into the resolution log. Two fixtures that both state the same
    height, or both leave it unstated (and so will both fall through to the same
    type's default), are unambiguously the same module either way.

    Only the *interior* gaps of a run close — each fixture's outward-facing edge
    (the two ends of the run) is left exactly as declared, so the run's total
    footprint never grows past the sum of its members' own footprints.
    """
    # index -> (type_key, yaw, along, perp, width, depth, height_or_None, elevation)
    info: dict[int, tuple[str, float, float, float, float, float, float | None]] = {}
    for i, f in enumerate(layout.fixtures):
        w = positive_float(f.width_mm)
        d = positive_float(f.depth_mm)
        type_key, _, _ = ftypes.resolve_fixture_type(
            f.label, f.kind, w, d, positive_float(f.height_mm)
        )
        if type_key not in _MERGEABLE_FIXTURE_TYPES:
            continue
        if w is None:
            w = ftypes.get_type(type_key).default_w
        yaw = math.radians(f.rotation_deg or 0.0)
        along = f.x * math.cos(yaw) + f.y * math.sin(yaw)
        perp = -f.x * math.sin(yaw) + f.y * math.cos(yaw)
        info[i] = (type_key, f.rotation_deg or 0.0, along, perp, w, d or 0.0, f.height_mm)

    # Group into candidate rows: same type, same rotation, same perpendicular
    # offset, and matching declared height (None matches None — see docstring).
    rows: dict[tuple[str, float, float, float | None], list[int]] = {}
    for i, (type_key, rot, along, perp, w, d, h) in info.items():
        row_key = None
        for existing in rows:
            e_type, e_rot, e_perp, e_h = existing
            if (
                e_type == type_key
                and abs(e_rot - rot) < 1e-3
                and abs(e_perp - perp) <= _RUN_ROW_TOLERANCE_MM
                and (e_h == h or (e_h is not None and h is not None and abs(e_h - h) < 1.0))
            ):
                row_key = existing
                break
        if row_key is None:
            row_key = (type_key, rot, perp, h)
            rows[row_key] = []
        rows[row_key].append(i)

    extensions: dict[int, tuple[float, float, int, int, bool, bool]] = {}
    run_id = 0
    for members in rows.values():
        members.sort(key=lambda i: info[i][2])  # sort by `along`
        # Split into runs on gaps beyond tolerance.
        run: list[int] = [members[0]]
        for prev_i, cur_i in zip(members, members[1:]):
            prev_along, prev_w = info[prev_i][2], info[prev_i][4]
            cur_along, cur_w = info[cur_i][2], info[cur_i][4]
            gap = (cur_along - cur_w / 2.0) - (prev_along + prev_w / 2.0)
            if -_RUN_GAP_TOLERANCE_MM <= gap <= _RUN_GAP_TOLERANCE_MM:
                run.append(cur_i)
                continue
            if len(run) >= 2:
                _record_run(run, info, extensions, run_id)
                run_id += 1
            run = [cur_i]
        if len(run) >= 2:
            _record_run(run, info, extensions, run_id)
            run_id += 1

    return extensions


def _record_run(
    run: list[int],
    info: dict[int, tuple[str, float, float, float, float, float, float | None]],
    extensions: dict[int, tuple[float, float, int, int, bool, bool]],
    run_id: int,
) -> None:
    size = len(run)
    for k, i in enumerate(run):
        extend_neg = 0.0
        extend_pos = 0.0
        if k > 0:
            j = run[k - 1]
            gap = (info[i][2] - info[i][4] / 2.0) - (info[j][2] + info[j][4] / 2.0)
            extend_neg = max(0.0, gap) / 2.0
        if k < size - 1:
            j = run[k + 1]
            gap = (info[j][2] - info[j][4] / 2.0) - (info[i][2] + info[i][4] / 2.0)
            extend_pos = max(0.0, gap) / 2.0
        # is_run_start/is_run_end: this fixture's own true outer edge, as
        # opposed to an edge that abuts another member of the same run.
        # A per-fixture edge_w band (a wall bay's end pilaster) must only
        # render at a run's true outer edges -- rendering one at every
        # member's own edge doubles up into a visible seam at each interior
        # join, which reads as a defect (a grout line down the middle of
        # what's meant to look like one continuous run), not a frame.
        extensions[i] = (extend_neg, extend_pos, run_id, size, k == 0, k == size - 1)


def _build_fixture_meshes(
    f: Fixture,
    layout: UnifiedLayout,
    ceiling_mm: float,
    log: ResolutionLog,
    index: int,
    edge_extend_neg_mm: float = 0.0,
    edge_extend_pos_mm: float = 0.0,
    run_id: int | None = None,
    run_size: int = 1,
    is_run_start: bool = True,
    is_run_end: bool = True,
    profile: "vprofiles.VisualProfile | None" = None,
) -> tuple[list[tuple[str, trimesh.Trimesh, str | None]], dict[str, Any]]:
    """
    Build one fixture as a stack of bands.

    Returns ``([(band_name, mesh, material_key), ...], extras)``. A type with a
    single band is exactly the old behaviour — one box — so untyped fixtures are
    unchanged. Multi-band types read as cabinetry rather than as blocks.

    ``edge_extend_neg_mm``/``edge_extend_pos_mm`` come from ``_group_wall_bay_runs``
    and close the gap to this fixture's immediate neighbours in a merged run —
    they only ever affect *rendered* geometry, never ``extras["width_mm"]`` or the
    dimension label, so a run reads as continuous on screen while every fixture
    keeps its own declared size in the data (and in click-to-SKU).

    Bands are fractions of the fixture's own height, so a recipe scales to
    whatever dimensions the source declares; nothing here overrides a measured
    value.
    """
    subject = f"fixture[{index}]"
    w = positive_float(f.width_mm)
    d = positive_float(f.depth_mm)

    type_key, type_source, type_rule = ftypes.resolve_fixture_type(
        f.label, f.kind, w, d, positive_float(f.height_mm)
    )
    log.record(
        Resolution("fixture_type", type_key, type_source, type_rule, subject)
    )
    spec = ftypes.get_type(type_key)

    # Footprint: declared where present, otherwise the type's default.
    if w is None:
        w = spec.default_w
        log.record(
            Resolution("width_mm", w, SOURCE_TYPE_DEFAULT, "R-FXW-04", subject)
        )
    if d is None:
        d = spec.default_d
        log.record(
            Resolution("depth_mm", d, SOURCE_TYPE_DEFAULT, "R-FXD-04", subject)
        )

    height_mm, height_source = resolve_fixture_height_mm(f, type_key, log, subject)
    elevation_mm, elevation_source = resolve_fixture_elevation_mm(
        f, layout, type_key, log, subject
    )

    # Keep the fixture inside the room: drop the elevation before shortening the
    # box, so a declared height survives when possible.
    elevation_mm = max(0.0, min(elevation_mm, max(0.0, ceiling_mm - _EPS_MM)))
    height_mm = min(height_mm, ceiling_mm - elevation_mm)
    if height_mm <= _EPS_MM:
        return [], {}

    yaw = math.radians(f.rotation_deg or 0.0)
    out: list[tuple[str, trimesh.Trimesh, str | None]] = []

    # An authored mesh, where one exists and fits without distortion. Form is the
    # thing band recipes cannot supply — a chair is not a stack of boxes — so the
    # asset wins when it is usable, and the recipe stands in when it is not.
    fit = asset_loader.fit_asset(type_key, w, d, height_mm, spec.height_means)
    if fit is not None:
        placed = asset_loader.place(fit.mesh, f.x, f.y, elevation_mm, yaw)
        log.record(
            Resolution("geometry", fit.method, SOURCE_PARSED, fit.rule_id, subject)
        )
        extras = {
            "label": f.label or "",
            "kind": f.kind or "",
            "fixture_type": type_key,
            "type_source": type_source,
            "type_rule": type_rule,
            "height_source": height_source,
            "elevation_source": elevation_source,
            "width_mm": w,
            "depth_mm": d,
            "height_mm": height_mm,
            "elevation_mm": elevation_mm,
            "geometry_source": "authored_asset",
            "fit_method": fit.method,
            "fit_scale": round(fit.scale, 4),
        }
        return [("asset", placed, spec.bands[0].material)], extras

    # Wall-bay run merging: extend the footprint used for rendering toward
    # whichever neighbour(s) this fixture abuts, closing the small residual gap
    # so the run reads as continuous. Only the render footprint changes — `w`
    # itself, and everything derived from it in `extras` below, stays the
    # fixture's true declared size.
    w_render = w + edge_extend_neg_mm + edge_extend_pos_mm
    # Extending one edge more than the other moves the box's own centre; this is
    # the local along-the-wall offset that keeps both edges landing exactly
    # where the gap computation intended.
    along_shift_mm = (edge_extend_pos_mm - edge_extend_neg_mm) / 2.0

    for band in vprofiles.effective_bands(spec, profile):
        bz0 = elevation_mm + height_mm * band.z0
        bz1 = elevation_mm + height_mm * band.z1
        bh = bz1 - bz0
        if bh <= _EPS_MM:
            continue
        # Shadow-gap reveal: shave the top, never the bottom, so the band's
        # declared start (bz0) is untouched -- purely a subtraction, same as
        # every other allowance in this loop. Skipped when this band's top IS
        # the fixture's own top (z1 == 1.0): that is the "ends exactly where
        # the BOQ says it does" face, not a seam against another band, and
        # every recipe that relies on the fixture's declared height being
        # exact (island_table's product sitting flush with its own top, a
        # cabinet's worktop, ...) would quietly fall short of it otherwise.
        # Clamped to a quarter of the band's own height so a thin band (a
        # handle groove, a shelf lip) keeps most of its own thickness.
        if band.z1 < 1.0 - 1e-9:
            bh -= min(_BAND_REVEAL_MM, bh * 0.25)
        if bh <= _EPS_MM:
            continue
        # Width only ever shrinks, so no recipe can widen the model's X/Y bounds
        # past the layout's. Depth may grow forward, but only as far as the
        # type's declared POC envelope -- clamped here rather than trusted, so a
        # recipe cannot quietly take more room than its type admits to.
        bw = w_render * (1.0 - max(0.0, band.inset_w))
        bd = d * (1.0 - max(0.0, band.inset_d))
        project = max(0.0, band.project_d)
        if project > 0.0:
            envelope_d = d * max(1.0, spec.poc_envelope_d)
            bd = min(bd + d * project, envelope_d)
        if bw <= _EPS_MM or bd <= _EPS_MM:
            continue
        # A "back" band sits against the rear of the footprint, which is what
        # puts a backpanel behind a cabinet rather than through it.
        #
        # A projecting band pins the opposite face and grows the other way, and
        # the sign matters more than it looks: these fixtures face +y in their
        # own frame, so growing the same way as a "back" band sends shelves
        # straight through the wall the bay hangs on. Measured, not assumed --
        # the first version of this did exactly that.
        if project > 0.0:
            shift = (bd - d) / 2.0
        elif band.back:
            shift = (d - bd) / 2.0
        elif band.recess_mm > 0.0:
            # Same direction as "back" (toward the wall side), just a small
            # explicit distance instead of "flush to the rear" -- glass set
            # back from its frame, or a groove pressed into a drawer front.
            # Clamped to the clearance inset_d already freed up, so this can
            # only use room the band gave up, never push past the un-inset
            # footprint's own front face.
            shift = min(band.recess_mm, max(0.0, d - bd) / 2.0)
        else:
            shift = 0.0
        # A band's cells: either a repeat grid (six phones on a table, four
        # boxes on a shelf) or -- for edge_w -- exactly two copies pinned to
        # the footprint's left/right edges (end pilasters; see edge_w's own
        # docstring for why a grid can't produce this). Each cell is
        # (piece_w, piece_d, off_w, off_d, across_base, label); the mesh-build
        # step below is shared by both, so a placed-asset catalog and every
        # box fallback work identically regardless of which produced the cell.
        cells: list[tuple[float, float, float, float, float, str | None]] = []
        if band.edge_w > 0.0:
            piece_w = min(band.edge_w, w_render / 2.0 - _EPS_MM)
            if piece_w > _EPS_MM:
                edge_off = (w_render - piece_w) / 2.0
                # Only at a run's true outer edges (see _record_run) -- an
                # interior join between two merged fixtures gets no pilaster
                # from either side, so a run still reads as one continuous
                # bay rather than a seam at every join.
                if is_run_start:
                    cells.append((piece_w, bd, -edge_off, 0.0, shift, "l"))
                if is_run_end:
                    cells.append((piece_w, bd, edge_off, 0.0, shift, "r"))
        else:
            # The footprint is divided into cells and the inset shrinks each
            # copy *within its own cell*, which is what leaves a gap between
            # neighbours. Applying the inset to the whole span instead makes
            # the copies tile edge to edge and fuse back into one solid
            # block, which on a table top reads as a window frame rather
            # than as product.
            #
            # At 1 x 1 the cell is the footprint, so this is exactly the
            # single centred band every recipe had before repeats existed.
            nw = max(1, band.repeat_w)
            nd = max(1, band.repeat_d)
            for iw in range(nw):
                for jd in range(nd):
                    if nw > 1:
                        cell_w = w_render / nw
                        piece_w = cell_w * (1.0 - max(0.0, band.inset_w))
                        off_w = ((iw + 0.5) / nw - 0.5) * w_render
                    else:
                        piece_w, off_w = bw, 0.0
                    if nd > 1:
                        # Depth repeats spread across the declared footprint
                        # and ignore back-anchoring and projection: no
                        # recipe needs a grid that also grows out of its own
                        # footprint, and combining the two would make the
                        # envelope unreadable.
                        cell_d = d / nd
                        piece_d = cell_d * (1.0 - max(0.0, band.inset_d))
                        off_d, across_base = ((jd + 0.5) / nd - 0.5) * d, 0.0
                    else:
                        piece_d, off_d, across_base = bd, 0.0, shift
                    label = None if nw == 1 and nd == 1 else f"{iw}{jd}"
                    cells.append((piece_w, piece_d, off_w, off_d, across_base, label))

        # A tuple asset_key (or a profile's per-band catalog override) is a
        # small product catalog rather than one repeated mesh; each cell
        # picks deterministically from it by its own position so the model
        # stays reproducible (same input -> same vertex hash) without any
        # random state.
        catalog = vprofiles.catalog_for_band(band, profile)

        for ci, (piece_w, piece_d, off_w, off_d, across_base, label) in enumerate(cells):
            if piece_w <= _EPS_MM or piece_d <= _EPS_MM:
                continue
            # Depth-direction shift (back-anchoring or projection) plus the
            # along-wall shift from run-merging and the cell offset — all in
            # the fixture's own frame, rotated into world X/Y together.
            along = along_shift_mm + off_w
            across = across_base + off_d
            cx = f.x - math.sin(yaw) * across + math.cos(yaw) * along
            cy = f.y + math.cos(yaw) * across + math.sin(yaw) * along
            centre = (cx, cy, bz0 + bh / 2.0)
            mesh = None
            if catalog:
                cell_asset_key = catalog[(index * 131 + ci * 17) % len(catalog)]
                # A placed asset rests its base on the cell floor (bz0), not
                # centred on the band slice the way a box is -- a real
                # object's height is its own, not stretched to fill a
                # declared band height.
                mesh = _place_prop_in_cell(
                    cell_asset_key,
                    cell_w=piece_w, cell_d=piece_d,
                    lay_flat=band.asset_lay_flat,
                    tilt_deg=band.asset_tilt_deg,
                    target_size_mm=band.asset_target_mm,
                    centre=(cx, cy, bz0), yaw_rad=yaw,
                )
            used_asset = mesh is not None
            if mesh is None and band.corner_r > 0.0:
                mesh = _rounded_box(
                    piece_w, piece_d, bh, centre=centre, yaw_rad=yaw,
                    radius=min(piece_w, piece_d) * band.corner_r,
                )
            elif mesh is None and band.bevel_mm > 0.0:
                mesh = _bevelled_top_box(
                    piece_w, piece_d, bh, centre=centre, yaw_rad=yaw,
                    bevel=band.bevel_mm,
                )
            elif mesh is None:
                mesh = _box(piece_w, piece_d, bh, centre=centre, yaw_rad=yaw)
            if mesh is None:
                continue
            name = band.name if label is None else f"{band.name}_{label}"
            # None tells add() to keep this mesh's own visual (its
            # placed-asset texture) instead of overwriting it with the
            # band's palette material -- the same signal the whole-fixture
            # authored-asset path would need if one ever carried a texture.
            out.append((name, mesh, None if used_asset else band.material))

    extras = {
        "label": f.label or "",
        "kind": f.kind or "",
        "fixture_type": type_key,
        "type_source": type_source,
        "type_rule": type_rule,
        "height_source": height_source,
        "elevation_source": elevation_source,
        "width_mm": w,
        "depth_mm": d,
        "height_mm": height_mm,
        "elevation_mm": elevation_mm,
        "geometry_source": "parametric_bands",
        "wants_asset": spec.needs_asset,
        "run_id": run_id,
        "run_size": run_size,
    }
    return out, extras


def _room_slabs(
    layout: UnifiedLayout, thickness_mm: float, z_base: float
) -> list[tuple[trimesh.Trimesh, str, int]]:
    """
    Extrude room polygons into slabs at ``z_base``.

    Rooms repeated verbatim are skipped. Parsers that read the same plan from
    more than one table (the PDF sheet path does this) emit each room twice, and
    stacking identical coplanar slabs doubles the geometry and z-fights in the
    viewer. The room's own index is carried through so a slab can be traced back
    to the room it came from — dedupe otherwise breaks that correspondence.
    """
    out: list[tuple[trimesh.Trimesh, str, int]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for room_index, room in enumerate(layout.rooms):
        pts = [(float(p[0]), float(p[1])) for p in room.polygon if len(p) >= 2]
        if len(pts) < 3:
            continue
        key = tuple(pts)
        if key in seen:
            continue
        seen.add(key)
        poly = SPolygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= _EPS_MM or not isinstance(poly, SPolygon):
            continue
        try:
            # engine="earcut" is mandatory: the alternative `triangle` engine is
            # not licensed for commercial use.
            slab = trimesh.creation.extrude_polygon(poly, thickness_mm, engine="earcut")
        except Exception:
            logger.warning("slab failed for room %r", room.name, exc_info=True)
            continue
        slab.apply_translation((0.0, 0.0, z_base))
        out.append((slab, room.name or "", room_index))
    return out


def _build_ceiling_meshes(
    layout: UnifiedLayout, ceiling_mm: float
) -> list[tuple[trimesh.Trimesh, str, int]]:
    """Thin slabs from room polygons, sitting at ceiling level."""
    return _room_slabs(
        layout, _CEILING_SLAB_THICKNESS_MM, ceiling_mm - _CEILING_SLAB_THICKNESS_MM
    )


def _build_floor_meshes(
    layout: UnifiedLayout,
) -> list[tuple[trimesh.Trimesh, str, int]]:
    """
    Floor plate, sitting just below z=0 so it never z-fights with fixtures.

    Page-3-style views read as a room largely because there is a floor under
    everything; without one the model reads as objects hanging in space.
    """
    return _room_slabs(layout, _FLOOR_SLAB_THICKNESS_MM, -_FLOOR_SLAB_THICKNESS_MM)


# ---------------------------------------------------------------------------
# Dimension annotations
# ---------------------------------------------------------------------------


def _build_dimensions(
    layout: UnifiedLayout,
    *,
    ceiling_mm: float,
) -> list[dict[str, Any]]:
    """
    Dimension callouts as data, not geometry.

    Emitted into glTF ``extras`` so the viewer draws lines and labels with real
    text. Baking text into a mesh would need a font dependency and would not be
    readable from every angle. The field names mirror ``meta.svg_dimensions``
    in ``geometry_engine`` so both renderers read the same shape, with ``z``
    added.
    """
    dims: list[dict[str, Any]] = []

    for i, w in enumerate(layout.walls):
        length = _wall_length(w)
        if length <= _EPS_MM:
            continue
        # Outward normal in plan, used by the viewer to offset the dim line.
        nx, ny = -(w.y2 - w.y1) / length, (w.x2 - w.x1) / length
        dims.append(
            {
                "group": GROUP_WALLS,
                "ref": f"wall_{i}",
                "x1": w.x1, "y1": w.y1,
                "x2": w.x2, "y2": w.y2,
                "z": 0.0,
                "nx": nx, "ny": ny,
                "label": f"{length:.0f}",
                "kind": "wall_length",
            }
        )

    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    if maxx > minx and maxy > miny:
        dims.append(
            {
                "group": "overall",
                "ref": "overall_width",
                "x1": minx, "y1": miny, "x2": maxx, "y2": miny,
                "z": 0.0, "nx": 0.0, "ny": -1.0,
                "label": f"{maxx - minx:.0f}",
                "kind": "overall_width",
            }
        )
        dims.append(
            {
                "group": "overall",
                "ref": "overall_depth",
                "x1": maxx, "y1": miny, "x2": maxx, "y2": maxy,
                "z": 0.0, "nx": 1.0, "ny": 0.0,
                "label": f"{maxy - miny:.0f}",
                "kind": "overall_depth",
            }
        )
        dims.append(
            {
                "group": GROUP_CEILING,
                "ref": "ceiling_height",
                "x1": minx, "y1": miny, "x2": minx, "y2": miny,
                "z": ceiling_mm, "nx": -1.0, "ny": 0.0,
                "label": f"{ceiling_mm:.0f}",
                "kind": "ceiling_height",
            }
        )

    for i, f in enumerate(layout.fixtures):
        if f.width_mm is None or f.depth_mm is None:
            continue
        if f.width_mm <= 0 or f.depth_mm <= 0:
            continue
        type_key, _, _ = ftypes.resolve_fixture_type(
            f.label, f.kind, f.width_mm, f.depth_mm, f.height_mm
        )
        height_mm, _ = resolve_fixture_height_mm(f, type_key)
        elevation_mm, _ = resolve_fixture_elevation_mm(f, layout, type_key)
        label = f"{f.width_mm:.0f} x {f.depth_mm:.0f} x {height_mm:.0f}"
        if elevation_mm > _EPS_MM:
            # Mounted fixtures read wrongly without their elevation stated.
            label += f" @ {elevation_mm:.0f}"
        dims.append(
            {
                "group": GROUP_FIXTURES,
                "ref": f"fixture_{i}",
                "x1": f.x, "y1": f.y, "x2": f.x, "y2": f.y,
                "z": elevation_mm + height_mm,
                "nx": 0.0, "ny": 0.0,
                "label": label,
                "kind": "fixture_extents",
                "fixture_label": f.label or "",
            }
        )

    return dims


def layout_bounds_2d(layout: UnifiedLayout) -> tuple[float, float, float, float]:
    """
    Plan bounds over the geometry the massing model actually extrudes.

    Deliberately not ``geometry_engine._bounds``: that one covers walls and
    rooms only, while the massing scene also contains fixture footprints.
    """
    xs: list[float] = []
    ys: list[float] = []
    for w in layout.walls:
        xs.extend([w.x1, w.x2])
        ys.extend([w.y1, w.y2])
    for r in layout.rooms:
        for p in r.polygon:
            if len(p) >= 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    for f in layout.fixtures:
        if f.width_mm and f.depth_mm and f.width_mm > 0 and f.depth_mm > 0:
            rad = math.radians(f.rotation_deg or 0.0)
            c, s = math.cos(rad), math.sin(rad)
            hw, hd = f.width_mm / 2.0, f.depth_mm / 2.0
            for lx, ly in ((-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)):
                xs.append(f.x + c * lx - s * ly)
                ys.append(f.y + s * lx + c * ly)
        else:
            xs.append(f.x)
            ys.append(f.y)
    if not xs or not ys:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Viewpoints
# ---------------------------------------------------------------------------

# Cardinal elevations, matching the A-D section markers dxf_exporter already
# draws (_draw_elevation_markers) and the ELEVATION-A/B/C convention retail BOQs
# use. Angles are the direction the camera looks, in plan.
_ELEVATION_VIEWS: tuple[tuple[str, str, float, float], ...] = (
    ("A", "North elevation", 0.0, -1.0),
    ("B", "East elevation", -1.0, 0.0),
    ("C", "South elevation", 0.0, 1.0),
    ("D", "West elevation", 1.0, 0.0),
)


def _plan_viewpoint(
    layout: UnifiedLayout,
    ceiling_mm: float,
    entrance: tuple[float, float, float] | None,
) -> dict[str, Any]:
    """
    The default camera, framed the way the reference proposal frames its own
    plan view: standing back from the entrance, looking straight down the length
    of the shop.

    ``entrance`` is ``(x, y, heading)`` for the shopfront wall — the same
    opening that colours the shopfront dark, so the model is read from the door
    a customer actually walks through rather than from whichever corner the
    bounding box happens to start at. A corner view puts one long wall edge-on
    and hides most of what is on it; looking down the axis shows both.

    Falls back to the previous corner axonometric when no entrance is
    identifiable, on the same principle as the shopfront colouring: derive it
    from a real signal or don't claim to know it.
    """
    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    width, depth = maxx - minx, maxy - miny
    radius = max(width, depth, ceiling_mm) or 1000.0
    target = [cx, cy, ceiling_mm * 0.35]

    if entrance is None:
        return {
            "id": "PLAN",
            "name": "Plan view",
            "kind": "axonometric",
            "target": target,
            # ~63 degrees above the plan, matching how design proposals present
            # a plan view: steep enough to read the whole floor, angled enough to
            # keep the fixtures three-dimensional. Below about 50 the near wall
            # occludes the interior entirely.
            "position": [cx + radius * 0.50, cy - radius * 0.50, radius * 1.45],
            "orthographic": True,
            "hide_groups": [GROUP_CEILING],
        }

    ex, ey, heading = entrance
    # Step outward along the entrance wall's outward normal, which is whichever
    # of its two perpendiculars points away from the centre of the plan.
    nx, ny = math.sin(heading), -math.cos(heading)
    if (ex - cx) * nx + (ey - cy) * ny < 0.0:
        nx, ny = -nx, -ny

    standoff = radius * 1.30
    nominal_z = target[2] + standoff * math.tan(math.radians(_PLAN_ELEVATION_DEG))

    # How much floor the entrance wall hides is fixed by similar triangles:
    # a camera at height H, a distance R back from a wall of height h, loses
    # R*h/(H-h) of the floor behind it. Invert that for the H that keeps the
    # loss inside the budget, and take whichever camera is higher.
    to_wall = max(standoff - max(width, depth) / 2.0, 1.0)
    budget = _PLAN_MAX_OCCLUSION * max(width, depth)
    clearing_z = ceiling_mm + to_wall * ceiling_mm / budget if budget > 0 else nominal_z

    return {
        "id": "PLAN",
        "name": "Plan view",
        "kind": "axonometric",
        "target": target,
        "position": [
            cx + nx * standoff,
            cy + ny * standoff,
            max(nominal_z, clearing_z),
        ],
        # Perspective, not orthographic: the reference plan view converges, and
        # convergence is most of what makes a long shop read as deep rather than
        # as a flat strip.
        "orthographic": False,
        "hide_groups": [GROUP_CEILING],
    }


def _entrance_eye_level_viewpoints(
    layout: UnifiedLayout,
    ceiling_mm: float,
    entrance: tuple[float, float, float],
    preset_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    """
    Eye-level interior presets walking a customer's own path in from the
    entrance -- the A-D elevations above stand *outside* the shell looking
    in, which reads fine for a roughly square room but frames a long, narrow
    one badly (a long side wall, seen end-on from far outside a short far
    wall, fills the whole camera regardless of standoff -- measured while
    building this, not guessed: see the module's own render QA notes). These
    three walk *inside* instead, along the entrance's own inward direction,
    so framing scales with the store's actual depth rather than the
    generic bounding radius.

    Only built from a real entrance signal, same principle as the PLAN
    camera's own fallback: derive it or don't claim to know it. A caller
    with no identifiable entrance simply doesn't get these presets.
    """
    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    ex, ey, heading = entrance
    # Inward unit vector: the wall's own outward normal, negated -- reuse
    # _plan_viewpoint's derivation so both stay consistent with each other.
    ix, iy = -math.sin(heading), math.cos(heading)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    # Keep whichever of the two perpendiculars actually points from the
    # entrance toward the plan's centre -- a plain dot product with the
    # entrance-to-centre vector, not a distance comparison.
    if ix * (cx - ex) + iy * (cy - ey) < 0.0:
        ix, iy = -ix, -iy
    jx, jy = -iy, ix  # perpendicular ("across" the aisle)

    # Depth of the store along the walk-in direction, projecting every
    # corner of the bounds onto (ix, iy) -- not just width/depth, since the
    # entrance wall is not guaranteed axis-aligned with the layout's bbox.
    corners = ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy))
    along = [(px - ex) * ix + (py - ey) * iy for px, py in corners]
    across = [(px - ex) * jx + (py - ey) * jy for px, py in corners]
    depth_in = max(along) if along else 3000.0
    half_width = (max(across) - min(across)) / 2.0 if across else 1500.0
    eye_z = min(1650.0, ceiling_mm * 0.55)

    def point(depth_frac: float, across_mm: float) -> list[float]:
        d = depth_in * depth_frac
        return [ex + ix * d + jx * across_mm, ey + iy * d + jy * across_mm, eye_z]

    presets = {
        "ENTRANCE": {
            "id": "ENTRANCE", "name": "Entrance view", "kind": "elevation",
            "position": point(0.06, 0.0), "target": point(0.85, 0.0),
        },
        "ANGLED": {
            "id": "ANGLED", "name": "Angled showroom view", "kind": "elevation",
            "position": point(0.30, half_width * 0.55),
            "target": point(0.80, -half_width * 0.55),
        },
        "REAR": {
            "id": "REAR", "name": "Rear / media wall view", "kind": "elevation",
            "position": point(0.55, 0.0), "target": point(0.98, 0.0),
        },
    }
    out = []
    for pid in preset_ids:
        vp = presets.get(pid)
        if vp is None:
            continue
        vp = dict(vp)
        vp["orthographic"] = False
        # Unlike PLAN/A-D (standing outside the shell looking in, where an
        # open "doll's-house" ceiling is the whole point), these three stand
        # *inside* the room at eye level -- a real customer sees a ceiling
        # overhead, and hiding it here reads as a blown-out hole in the
        # viewer rather than a deliberate cutaway (measured by rendering
        # and looking, not guessed). show_groups is the mirror of
        # hide_groups every other viewpoint already sets.
        vp["show_groups"] = [GROUP_CEILING]
        out.append(vp)
    return out


def build_viewpoints(
    layout: UnifiedLayout,
    ceiling_mm: float,
    entrance: tuple[float, float, float] | None = None,
    profile: "vprofiles.VisualProfile | None" = None,
) -> list[dict[str, Any]]:
    """
    Named cameras for the viewer, in layout coordinates.

    Design proposals present 3D as a plan view plus a handful of named
    elevations, each keyed to an arrow on a small plan. The DXF exporter already
    draws A-D markers for exactly that, so the 3D viewpoints reuse the same
    names and directions rather than inventing a second convention.

    A visual profile may additionally request eye-level interior presets
    (``profile.camera_presets``, e.g. "ENTRANCE"/"ANGLED"/"REAR") -- see
    ``_entrance_eye_level_viewpoints``. Opt-in and additive: a layout with no
    profile gets exactly the PLAN + A-D list this function always returned.
    """
    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    if maxx <= minx or maxy <= miny:
        return []

    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    width, depth = maxx - minx, maxy - miny
    radius = max(width, depth, ceiling_mm) or 1000.0
    eye_z = ceiling_mm * 0.55

    out: list[dict[str, Any]] = [_plan_viewpoint(layout, ceiling_mm, entrance)]
    if profile is not None and profile.camera_presets and entrance is not None:
        out.extend(
            _entrance_eye_level_viewpoints(layout, ceiling_mm, entrance, profile.camera_presets)
        )
    for vid, name, dx, dy in _ELEVATION_VIEWS:
        out.append(
            {
                "id": vid,
                "name": name,
                "kind": "elevation",
                "target": [cx, cy, eye_z],
                "position": [
                    cx - dx * radius * 1.25,
                    cy - dy * radius * 1.25,
                    eye_z,
                ],
                "direction": [dx, dy],
                "orthographic": False,
                "hide_groups": [GROUP_CEILING],
            }
        )
    return out


def turntable_viewpoints(
    layout: UnifiedLayout, ceiling_mm: float, n_frames: int
) -> list[dict[str, Any]]:
    """
    ``n_frames`` cameras spaced evenly around a full circle at constant
    height and distance, all aimed at the same target -- a 360-degree
    turntable, not a single view. Same elevation and standoff as the plan
    view's own no-entrance fallback (``_plan_viewpoint``), just swept through
    every azimuth instead of one fixed corner, so a turntable and the default
    plan view read as the same camera doing a lap, not two different shots.

    Used only by the photoreal render pipeline (``app/services/photoreal.py``)
    to pre-render a fixed sequence of stills for a looping animation -- there
    is no live camera on this path, so there is nothing here for the browser
    viewer to consume directly, unlike ``build_viewpoints``.
    """
    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    if maxx <= minx or maxy <= miny or n_frames <= 0:
        return []

    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    width, depth = maxx - minx, maxy - miny
    radius = max(width, depth, ceiling_mm) or 1000.0
    target = [cx, cy, ceiling_mm * 0.35]
    standoff = radius * 0.70
    eye_z = radius * 1.45

    out: list[dict[str, Any]] = []
    for i in range(n_frames):
        theta = 2.0 * math.pi * i / n_frames
        out.append(
            {
                "id": f"T{i:02d}",
                "name": f"Turntable frame {i}",
                "target": target,
                "position": [
                    cx + standoff * math.cos(theta),
                    cy + standoff * math.sin(theta),
                    eye_z,
                ],
                "orthographic": False,
                "hide_groups": [GROUP_CEILING],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Scene assembly
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _tinted_texture(material_key: str) -> Image.Image | None:
    """
    Load a tileable texture and retint it to this material's measured palette
    colour, or return None if the file is missing.

    The shipped file keeps its source tone; the tone that reaches the render
    comes from ``_PALETTE``. Rescaling the image's channel means onto the
    palette colour, rather than multiplying a texture against a coloured
    ``baseColorFactor``, keeps one authority for colour instead of two that
    multiply into something neither of them states. The palette entries were
    sampled off the reference proposal, so they, not a stock texture's own
    white balance, are what the model should match.

    Re-encoded as JPEG deliberately. trimesh picks the embedded image format
    from ``Image.format``, which any processed image loses, and defaults to
    PNG: the same 512px oak tile costs 301KB as PNG and 28KB as JPEG, against a
    GLB size budget that a test already guards.

    Cached per process — the retint and re-encode are pure functions of the
    file and the palette, and every mesh sharing a material shares one image
    object, which is what lets the exporter emit a single glTF image for it.
    """
    spec = _TEXTURED.get(material_key)
    if spec is None:
        return None
    path = _TEXTURE_DIR / spec.color
    if not path.is_file():
        logger.warning("texture %s missing at %s; falling back to flat colour", material_key, path)
        return None

    img = Image.open(path).convert("RGB")
    target = np.array(_PALETTE.get(material_key, _COLOUR_FALLBACK)[:3]) * 255.0
    pixels = np.asarray(img, dtype=np.float32)
    mean = pixels.reshape(-1, 3).mean(axis=0)
    # A channel that is black on average cannot be scaled to anything else.
    gain = np.where(mean > 1.0, target / np.maximum(mean, 1e-6), 1.0)
    retinted = np.clip(pixels * gain, 0, 255).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(retinted).save(buf, format="JPEG", quality=82, optimize=True)
    buf.seek(0)
    return Image.open(buf)


@lru_cache(maxsize=None)
def _map_texture(filename: str) -> Image.Image | None:
    """
    Load an ORM or normal map exactly as shipped — no retinting, since neither
    carries colour (an ORM's channels are occlusion/roughness/metalness; a
    normal map's are a tangent-space direction). Re-encoded through the same
    BytesIO round-trip as ``_tinted_texture`` so ``Image.format`` reads
    ``"JPEG"`` and trimesh doesn't silently fall back to a much larger PNG.
    ``subsampling=0`` (4:4:4) on every re-encode, here and on disk: JPEG chroma
    subsampling blurs channels together, and unlike a colour photo these three
    channels are independent data (occlusion/roughness/metal, or a normal's
    tangent-space X/Y/Z) — subsampling would bleed one into another.

    Returns ``None`` if the file is missing, so a material with color but no
    ORM/normal map (or a partly-missing install) degrades to scalar
    roughness rather than failing the whole material.
    """
    path = _TEXTURE_DIR / filename
    if not path.is_file():
        logger.warning("map %s missing at %s; skipping", filename, path)
        return None
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True, subsampling=0)
    buf.seek(0)
    return Image.open(buf)


def _planar_uv(
    mesh: trimesh.Trimesh, *, tile_w: float, tile_h: float, yaw: float | None
) -> np.ndarray:
    """
    One planar UV projection for the whole mesh, in real-world units.

    ``yaw=None`` projects world (x, y) — floor and ceiling slabs, which are read
    face-on from above. A yaw projects (distance along that heading, z) — wall
    boxes, read face-on from inside the room.

    A single projection per mesh is what keeps this cheap: every vertex gets one
    UV, so vertices stay shared and the box stays watertight. Proper box mapping
    would need each face to own its vertices, which would trade that guarantee
    away. The cost is that faces perpendicular to the projection streak — a
    wall's end caps, a slab's edges — and those are millimetres wide and face
    away from every viewpoint the model ships with.
    """
    v = mesh.vertices
    if yaw is None:
        u, w = v[:, 0], v[:, 1]
    else:
        u = v[:, 0] * math.cos(yaw) + v[:, 1] * math.sin(yaw)
        w = v[:, 2]
    return np.column_stack((u / tile_w, w / tile_h))


def _material(
    name: str,
    colour: tuple[float, float, float, float],
    emissive: tuple[float, float, float] | None = None,
    roughness: float = _ROUGHNESS_FALLBACK,
    texture: Image.Image | None = None,
    orm_texture: Image.Image | None = None,
    normal_texture: Image.Image | None = None,
) -> trimesh.visual.material.PBRMaterial:
    """
    Flat shade, or a tileable texture for the few surfaces large enough to show
    a repeat. Only floor and wall carry ``orm_texture``/``normal_texture`` — a
    100mm-deep fixture band shows too little of a tile for a normal map to
    read, so cabinetry etc. stay scalar-roughness flat colour.

    ``emissive`` stays a flat colour: a light box is lit in reality, and an
    unlit grey box in its place reads as switched off.

    ``baseColorFactor`` goes to white whenever a texture is supplied. glTF
    multiplies the two, so leaving the palette colour in both places would
    square it and render the surface far darker than the colour anyone wrote
    down — the tint is already baked into the image by ``_tinted_texture``.

    Note the two colour fields are not the same scale. trimesh's PBRMaterial
    treats ``baseColorFactor`` as sRGB uint8 (0-255) and normalises it on
    export, but ``emissiveFactor`` passes straight through to the glTF file,
    where the spec requires linear floats in [0, 1]. Handing it 0-255 values
    silently clips every channel to 1.0 in any spec-compliant viewer — a pale
    blue glow becomes a plain white square, with no error anywhere to catch it.

    ``orm_texture`` supplies both ``metallicRoughnessTexture`` (G=roughness,
    B=metal) and ``occlusionTexture`` (R=AO) from one packed image, per glTF's
    documented convention for sharing a texture across those two slots.
    ``roughnessFactor`` becomes a pure multiplier once a real roughness map is
    present, so it goes to 1.0 rather than the judgement-call scalar used
    everywhere else.
    """
    base = (1.0, 1.0, 1.0, colour[3]) if texture is not None else colour
    kwargs: dict[str, Any] = dict(
        name=name,
        baseColorFactor=[int(round(c * 255)) for c in base],
        metallicFactor=0.0,
        roughnessFactor=1.0 if orm_texture is not None else roughness,
    )
    if texture is not None:
        kwargs["baseColorTexture"] = texture
    if orm_texture is not None:
        kwargs["metallicRoughnessTexture"] = orm_texture
        kwargs["occlusionTexture"] = orm_texture
    if normal_texture is not None:
        kwargs["normalTexture"] = normal_texture
    if emissive is not None:
        kwargs["emissiveFactor"] = [max(0.0, min(1.0, c)) for c in emissive]
    return trimesh.visual.material.PBRMaterial(**kwargs)


def build_massing(layout: UnifiedLayout) -> MassingResult:
    """
    Build the Tier A massing scene for ``layout``.

    Deterministic: identical input yields an identical vertex hash, which is
    what makes regenerate-on-demand safe without persisting anything. Every
    value the layout does not carry is resolved through ``resolution`` and
    recorded, so an assumption is always visible rather than implicit.
    """
    log = ResolutionLog()
    ceiling_mm, ceiling_source = resolve_ceiling_height_mm(layout, log)
    thickness_mm, thickness_source = resolve_wall_thickness_mm(layout, log)
    # No entry at all when no profile is selected: "no profile requested" is
    # not an assumption the way a defaulted ceiling height is -- it costs the
    # model nothing to log correctly, but logging it as a rank-5
    # (global_default, confidence 0.20) resolution the way every other
    # unknown-field resolver here does would drag every model's
    # model_confidence down to 0.20 and flip not_for_construction on for
    # every layout that simply never asked for a visual profile. Only an
    # explicit selection is worth recording.
    profile, profile_source = vprofiles.resolve_visual_profile(layout)
    if profile is not None:
        log.record(
            Resolution("visual_profile", profile.key, SOURCE_USER, "R-VIS-01")
        )

    scene = trimesh.Scene()
    # Z-up (layout) -> Y-up (glTF). Applied once at the root so geometry keeps
    # layout coordinates and the bbox invariant stays checkable.
    scene.graph.update(
        frame_to=GROUP_ROOT,
        frame_from="world",
        matrix=trimesh.transformations.rotation_matrix(-math.pi / 2.0, (1.0, 0.0, 0.0)),
    )
    for group in GROUPS:
        scene.graph.update(frame_to=group, frame_from=GROUP_ROOT)

    materials = {}
    for key, colour in _PALETTE.items():
        spec = _TEXTURED.get(key)
        materials[key] = _material(
            f"pf_{key}",
            colour,
            _EMISSIVE.get(key),
            roughness=_ROUGHNESS.get(key, _ROUGHNESS_FALLBACK),
            texture=_tinted_texture(key),
            orm_texture=_map_texture(spec.orm) if spec is not None and spec.orm else None,
            normal_texture=_map_texture(spec.normal) if spec is not None and spec.normal else None,
        )

    def add(
        mesh: trimesh.Trimesh,
        group: str,
        name: str,
        material_key: str | None,
        extras: dict | None = None,
        uv_yaw: float | None = None,
    ) -> None:
        """
        ``uv_yaw`` only matters for a textured material: it is the heading the
        surface faces along, so a wall's texture runs with the wall rather than
        with world X. None projects from above, which is what a floor wants.

        ``material_key=None`` means the mesh already carries its own real
        visual (a placed authored asset with its own texture -- see
        product_phone_generic in the fixtures manifest) and it must survive
        untouched: every other caller hands in a palette key precisely so
        this function *can* stamp a flat neutral shade over whatever the
        mesh had, and that would destroy a placed asset's texture instead.
        """
        if material_key is None:
            if extras:
                mesh.metadata.update(extras)
            # Marks this node as a placed authored asset -- consumers that
            # care whether geometry is guaranteed watertight (this module's
            # own tests; a future export/repair step) can check for it
            # instead of pattern-matching node names.
            mesh.metadata["placed_asset"] = True
            scene.add_geometry(mesh, node_name=name, geom_name=name, parent_node_name=group)
            return
        texture_spec = _TEXTURED.get(material_key)
        uv = None
        if texture_spec is not None and _tinted_texture(material_key) is not None:
            uv = _planar_uv(
                mesh, tile_w=texture_spec.tile_w, tile_h=texture_spec.tile_h, yaw=uv_yaw
            )
        mesh.visual = trimesh.visual.TextureVisuals(
            uv=uv, material=materials.get(material_key, materials[ftypes.MAT_GENERIC])
        )
        if extras:
            mesh.metadata.update(extras)
        scene.add_geometry(mesh, node_name=name, geom_name=name, parent_node_name=group)

    bound = _bind_openings_to_walls(list(layout.walls), list(layout.openings))
    shopfront_walls = _identify_shopfront_walls(layout, bound)

    wall_parts = 0
    for i, w in enumerate(layout.walls):
        parts = _build_wall_meshes(
            w, bound.get(i, []), thickness_mm=thickness_mm, ceiling_mm=ceiling_mm
        )
        material_key = "wall_shopfront" if i in shopfront_walls else "wall"
        wall_yaw = math.atan2(w.y2 - w.y1, w.x2 - w.x1)
        for j, mesh in enumerate(parts):
            add(
                mesh, GROUP_WALLS, f"wall_{i}_{j}", material_key,
                {"wall_index": i, "is_shopfront": i in shopfront_walls},
                uv_yaw=wall_yaw,
            )
        wall_parts += len(parts)

    fixture_count = 0
    fixture_bands = 0
    fixtures_with_assets = 0
    type_counts: dict[str, int] = {}
    typed = 0
    wall_bay_runs = _group_wall_bay_runs(layout)
    merged_run_ids: set[int] = set()
    for i, f in enumerate(layout.fixtures):
        extend_neg, extend_pos, run_id, run_size, is_run_start, is_run_end = (
            wall_bay_runs.get(i, (0.0, 0.0, None, 1, True, True))
        )
        bands, extras = _build_fixture_meshes(
            f, layout, ceiling_mm, log, i, extend_neg, extend_pos, run_id, run_size,
            is_run_start=is_run_start, is_run_end=is_run_end, profile=profile,
        )
        if not bands:
            continue
        type_key = extras.get("fixture_type", ftypes.FALLBACK_TYPE)
        type_counts[type_key] = type_counts.get(type_key, 0) + 1
        if type_key != ftypes.FALLBACK_TYPE:
            typed += 1
        if extras.get("geometry_source") == "authored_asset":
            fixtures_with_assets += 1
        if run_id is not None:
            merged_run_ids.add(run_id)
        for band_name, mesh, material_key in bands:
            add(
                mesh,
                GROUP_FIXTURES,
                f"fixture_{i}_{band_name}",
                material_key,
                {**extras, "band": band_name, "fixture_index": i},
            )
        fixture_count += 1
        fixture_bands += len(bands)

    ceiling_slabs = _build_ceiling_meshes(layout, ceiling_mm)
    for i, (mesh, room_name, room_index) in enumerate(ceiling_slabs):
        add(
            mesh,
            GROUP_CEILING,
            f"ceiling_{i}",
            "ceiling",
            {"room": room_name, "room_index": room_index},
        )

    floor_slabs = _build_floor_meshes(layout)
    for i, (mesh, room_name, room_index) in enumerate(floor_slabs):
        add(
            mesh,
            GROUP_FLOOR,
            f"floor_{i}",
            "floor",
            {"room": room_name, "room_index": room_index},
        )

    dimensions = _build_dimensions(layout, ceiling_mm=ceiling_mm)
    # Lowest index when several walls carry the shopfront, so the default camera
    # is as deterministic as the geometry it frames.
    entrance = None
    if shopfront_walls:
        w = layout.walls[min(shopfront_walls)]
        entrance = (
            (w.x1 + w.x2) / 2.0,
            (w.y1 + w.y2) / 2.0,
            math.atan2(w.y2 - w.y1, w.x2 - w.x1),
        )
    viewpoints = build_viewpoints(layout, ceiling_mm, entrance, profile)

    # z_source stays for backward compatibility with the existing viewer badge;
    # model_confidence is the fuller picture.
    z_source = "user_supplied" if ceiling_source == SOURCE_USER else "assumed_default"
    provenance = {
        "model_tier": "massing",
        "fixture_geometry": "mixed",
        "z_source": z_source,
        "ceiling_height_mm": ceiling_mm,
        "ceiling_height_source": ceiling_source,
        "wall_thickness_mm": thickness_mm,
        "wall_thickness_source": thickness_source,
        "fixtures_typed": typed,
        "fixtures_with_assets": fixtures_with_assets,
        "fixtures_untyped": fixture_count - typed,
        "fixture_type_counts": dict(sorted(type_counts.items())),
        # Stated out loud because it is the one place the model deliberately
        # draws outside the dimensions its source gave, and anyone measuring the
        # GLB against the BOQ deserves to find out here rather than by surprise.
        "poc_depth_projection": {
            key: {
                "declared_depth_is": "the BOQ product, unchanged",
                "rendered_depth_up_to": f"{spec.poc_envelope_d:g}x declared",
                "why": "a wall bay's stated depth is its back panel; the shelves "
                       "standing in front of it are not in the BOQ at all",
            }
            for key, spec in sorted(ftypes.FIXTURE_TYPES.items())
            if any(b.project_d > 0.0 for b in spec.bands)
            and type_counts.get(key)
        },
        "units": layout.units,
        "up_axis": "Y",
        "geometry_up_axis": "Z",
        # Only a blind (rank-5) default invalidates the model. A type default is
        # informed; an assumed ceiling height is not, and that is the one
        # unbounded, undetectable error in the whole pipeline.
        "not_for_construction": log.has_blind_defaults,
        **log.summary(),
    }

    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    stats = {
        "wall_segments": len(layout.walls),
        "wall_boxes": wall_parts,
        "openings_bound": sum(len(v) for v in bound.values()),
        "openings_total": len(layout.openings),
        "fixtures": fixture_count,
        "fixture_bands": fixture_bands,
        "wall_bay_runs": len(merged_run_ids),
        "shopfront_walls": sorted(shopfront_walls),
        "ceiling_slabs": len(ceiling_slabs),
        "floor_slabs": len(floor_slabs),
        "bounds_2d": [minx, miny, maxx, maxy],
    }

    scene.metadata.update(
        {
            "planforge": {
                "provenance": provenance,
                "dimensions": dimensions,
                "viewpoints": viewpoints,
                "stats": stats,
                "groups": list(GROUPS),
            }
        }
    )

    return MassingResult(
        scene=scene,
        provenance=provenance,
        dimensions=dimensions,
        stats=stats,
        viewpoints=viewpoints,
        resolutions=log.as_list(),
    )


def layout_to_glb(layout: UnifiedLayout) -> bytes:
    """Convenience wrapper: UnifiedLayout → GLB bytes, nothing written to disk."""
    return build_massing(layout).export_glb()
