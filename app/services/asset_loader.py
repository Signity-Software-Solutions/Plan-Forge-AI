"""
Authored fixture assets: load, normalise, and fit to a declared size.

Parametric band recipes get a fixture's dimensions right but its *form* wrong —
a chair is not a stack of boxes. Where a real mesh exists this module supplies
it; where one does not, or where using it would distort, the caller falls back
to the recipe.

The three problems this solves, in order of how often they bite:

**Normalisation.** An authored asset has an arbitrary origin, up-axis and unit.
Dropped in as-is it lands through the floor at the wrong size. Every asset
declares its native units and up-axis in ``manifest.json`` and is converted on
load to the pipeline's convention: millimetres, Z-up, footprint centred on the
origin, underside at z=0.

**Fitting.** An asset is one fixed size; fixtures vary. Scaling a mesh
non-uniformly turns round handles into ovals and skews chamfers, so it is not
something to do quietly. See ``fit_asset`` for the ladder.

**Provenance.** These files ship inside client deliverables, so licence and
source live in the manifest and every fit records how it was achieved.

Not yet implemented: tiling a run from a repeated module (``FIT_TILED``), which
is the correct answer for cabinetry and needs modular assets we do not have. The
constants are defined so the vocabulary is fixed now rather than invented twice.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

logger = logging.getLogger(__name__)

_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "fixtures"
_MANIFEST = _ASSET_DIR / "manifest.json"

# How each fit was achieved. Ordered best-first; the rule ids match the
# resolution ladder's convention so they read the same way in an audit.
FIT_EXACT = "exact"                  # R-FIT-01  declared size == authored size
FIT_TILED = "tiled"                  # R-FIT-02  integer multiple of a module
FIT_TILED_FILLER = "tiled_filler"    # R-FIT-03  tiled plus a filler panel
FIT_NINE_SLICE = "nine_slice"        # R-FIT-04  stretch the middle, keep the caps
FIT_UNIFORM = "uniform_scale"        # R-FIT-05  uniform, within tolerance
FIT_REJECTED = "rejected"            # R-FIT-06  would distort; use the recipe

_FIT_RULES = {
    FIT_EXACT: "R-FIT-01",
    FIT_TILED: "R-FIT-02",
    FIT_TILED_FILLER: "R-FIT-03",
    FIT_NINE_SLICE: "R-FIT-04",
    FIT_UNIFORM: "R-FIT-05",
    FIT_REJECTED: "R-FIT-06",
}

# Within this the authored size counts as the declared size — no scaling at all.
_EXACT_TOLERANCE = 0.01  # 1%

# Beyond this, a uniform scale has stopped being a resize and started being a
# different object. A 900mm chair scaled to 1400mm is not a tall chair, it is
# wrong, and a clean parametric box is the better answer.
_UNIFORM_LIMIT = 0.35  # 35%

# Non-uniform scaling is never applied. Chamfers, radii and handle spacing all
# distort visibly well before 5%, and unlike a wrong size it cannot be
# un-seen — so the ladder tiles, nine-slices, or gives up instead.

_UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}

# Reentrant: load_asset holds the lock and then calls _load_manifest, which
# takes it again. A plain Lock deadlocks there.
_lock = threading.RLock()
_manifest_cache: dict[str, Any] | None = None
_mesh_cache: dict[str, trimesh.Trimesh | None] = {}


@dataclass(frozen=True)
class AssetFit:
    """A placed asset plus how it was made to fit."""

    mesh: trimesh.Trimesh
    method: str
    rule_id: str
    scale: float

    @property
    def is_exact(self) -> bool:
        return self.method == FIT_EXACT


def _load_manifest() -> dict[str, Any]:
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache
    with _lock:
        if _manifest_cache is None:
            try:
                _manifest_cache = json.loads(_MANIFEST.read_text()).get("assets", {})
            except (OSError, ValueError):
                logger.warning("no usable asset manifest at %s", _MANIFEST, exc_info=True)
                _manifest_cache = {}
    return _manifest_cache


def available_assets() -> dict[str, Any]:
    """Manifest entries, for provenance reporting and tests."""
    return dict(_load_manifest())


def native_pose(type_key: str) -> str:
    """
    ``"standing"`` (default) or ``"resting"``.

    Every normalised asset here is Z-up and floor-at-zero, but that alone
    doesn't say which way an object's own silhouette naturally faces. Most of
    this manifest -- a chair, a phone standing in its authored orientation --
    naturally stands taller than it is wide, and ``Band.asset_lay_flat``
    (see mesh_builder._place_prop_in_cell) tips that onto its back for a
    table placement. An asset that has no sensible "standing" pose at all
    (an open laptop, physically an L-shape with no thin axis to stand on)
    is normalised already resting in its natural table pose instead, and
    declares ``"native_pose": "resting"`` in its manifest entry so a caller
    requesting asset_lay_flat on a catalog that happens to include it does
    not tip an already-correct pose into a wrong one.
    """
    entry = _load_manifest().get(type_key, {})
    pose = str(entry.get("native_pose", "standing")).lower()
    return pose if pose in ("standing", "resting") else "standing"


def _normalise(mesh: trimesh.Trimesh, entry: dict[str, Any]) -> trimesh.Trimesh:
    """Convert an authored mesh to millimetres, Z-up, footprint-centred, floor at 0."""
    mesh = mesh.copy()

    scale = _UNIT_TO_MM.get(str(entry.get("native_units", "m")).lower(), 1000.0)
    if scale != 1.0:
        mesh.apply_scale(scale)

    if str(entry.get("native_up_axis", "Y")).upper() == "Y":
        # Y-up (glTF) -> Z-up (layout). +90 about X, the inverse of what the
        # scene root applies on export.
        mesh.apply_transform(
            trimesh.transformations.rotation_matrix(math.pi / 2.0, (1.0, 0.0, 0.0))
        )

    lo, hi = mesh.bounds
    mesh.apply_translation((-(lo[0] + hi[0]) / 2.0, -(lo[1] + hi[1]) / 2.0, -lo[2]))
    return mesh


def load_asset(type_key: str) -> trimesh.Trimesh | None:
    """
    Normalised mesh for a fixture type, or ``None`` if there isn't a usable one.

    ``None`` is a first-class outcome, not an error: a missing or broken asset
    must degrade to the parametric recipe rather than break the model. Results
    are cached per process — assets are immutable for the life of a deploy.
    """
    if type_key in _mesh_cache:
        return _mesh_cache[type_key]

    with _lock:
        if type_key in _mesh_cache:
            return _mesh_cache[type_key]

        entry = _load_manifest().get(type_key)
        result: trimesh.Trimesh | None = None
        if entry:
            path = _ASSET_DIR / str(entry.get("file", ""))
            try:
                loaded = trimesh.load(path, force="mesh")
                if isinstance(loaded, trimesh.Trimesh) and len(loaded.faces):
                    result = _normalise(loaded, entry)
                else:
                    logger.warning("asset %s loaded empty", path)
            except Exception:
                logger.warning("asset %s failed to load", path, exc_info=True)
        _mesh_cache[type_key] = result
    return result


def fit_asset(
    type_key: str,
    width_mm: float,
    depth_mm: float,
    height_mm: float,
    height_means: str = "overall",
) -> AssetFit | None:
    """
    Fit an asset to a declared size, or decline.

    Height drives the scale. It is the dimension sources most reliably carry —
    the sample BOQ gives seating as ``— × — × 600``, height only — and it is the
    one people judge a chair by.

    But a uniform scale that passes the height check can still produce a badly
    proportioned object: an asset authored narrow-and-deep, scaled up to the
    right height, stays narrow-and-deep — a 372x1078mm shelf scaled to the right
    height becomes a 502x1455mm shelf, four times deeper than a 350mm-deep
    fixture should be. Uniform scaling preserves whatever proportions the source
    had; it does not correct them. So the resulting footprint is checked against
    the declared one too, whenever one was declared.

    Returns ``None`` when there is no asset, when the height fit would distort
    beyond ``_UNIFORM_LIMIT``, or when the resulting footprint would. Declining
    is the point: a correct parametric box beats a badly proportioned mesh,
    because a wrong size reads as schematic while a distorted object reads as
    broken.
    """
    mesh = load_asset(type_key)
    if mesh is None:
        return None

    overall = float((mesh.bounds[1] - mesh.bounds[0])[2])
    native = overall
    if height_means == "seat":
        # Retail schedules quote seat height for seating. Comparing a 450mm seat
        # height against a 1007mm overall height is comparing two different
        # measurements, and rejects an asset that in fact fits well.
        seat = _load_manifest().get(type_key, {}).get("seat_height_mm")
        if seat:
            native = float(seat)
        else:
            logger.debug("%s declares seat heights but has no seat_height_mm", type_key)
    if native <= 0 or height_mm <= 0:
        return None

    ratio = height_mm / native
    if abs(ratio - 1.0) <= _EXACT_TOLERANCE:
        return AssetFit(mesh.copy(), FIT_EXACT, _FIT_RULES[FIT_EXACT], 1.0)

    if abs(ratio - 1.0) > _UNIFORM_LIMIT:
        logger.debug(
            "asset %s declined: %.0fmm needed vs %.0fmm authored (%.0f%%)",
            type_key, height_mm, native, (ratio - 1.0) * 100,
        )
        return None

    # Check the footprint this scale would produce, not just the height. Uniform
    # scaling is proportion-preserving, not proportion-correcting: a source
    # authored with the wrong aspect ratio stays wrong at any scale.
    if width_mm > 0 or depth_mm > 0:
        native_w = float((mesh.bounds[1] - mesh.bounds[0])[0])
        native_d = float((mesh.bounds[1] - mesh.bounds[0])[1])
        scaled_w, scaled_d = native_w * ratio, native_d * ratio
        for declared, scaled, axis in ((width_mm, scaled_w, "W"), (depth_mm, scaled_d, "D")):
            if declared <= 0:
                continue
            deviation = abs(scaled - declared) / declared
            if deviation > _UNIFORM_LIMIT:
                logger.debug(
                    "asset %s declined: %s %.0fmm at h-fit vs %.0fmm declared (%.0f%%)",
                    type_key, axis, scaled, declared, deviation * 100,
                )
                return None

    fitted = mesh.copy()
    fitted.apply_scale(ratio)  # uniform: proportions survive
    return AssetFit(fitted, FIT_UNIFORM, _FIT_RULES[FIT_UNIFORM], ratio)


def place(mesh: trimesh.Trimesh, x: float, y: float, z: float, yaw_rad: float) -> trimesh.Trimesh:
    """Rotate about Z and move a normalised asset into the layout."""
    out = mesh.copy()
    transform = trimesh.transformations.rotation_matrix(yaw_rad, (0.0, 0.0, 1.0))
    transform[:3, 3] = (x, y, z)
    out.apply_transform(transform)
    return out


def _reset_caches() -> None:
    """Test hook only."""
    global _manifest_cache
    with _lock:
        _manifest_cache = None
        _mesh_cache.clear()
