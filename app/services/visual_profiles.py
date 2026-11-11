"""
Reusable "visual configuration" profiles layered on top of the generic fixture
taxonomy.

Why this exists
    The generic band recipes in ``fixture_types.py`` are deliberately
    company-neutral -- they describe shapes a retail interior is made of, not
    any one project's look. Getting closer to a *specific* reference (denser
    product population, project-specific framing details like end pilasters,
    a distinct material/lighting mood) is a different kind of decision: it is
    a styling choice for one engagement, not a fact about what a "wall bay" is
    everywhere. Mixing the two would make the taxonomy stop being reusable.

    A ``VisualProfile`` is that separation: an optional, named bundle of
    additive band tweaks, a product catalog, camera presets and a lighting
    hint, selected per-layout through the existing ``meta`` escape hatch
    (``layout.meta["visual_profile"] = "retail_showroom"``) the same
    way every other assumption in this pipeline is opted into -- no schema
    change, and every project that doesn't ask for one renders exactly as it
    did before this module existed.

Precedence, mirroring resolution.py's own ladder
    user_supplied   layout.meta["visual_profile"] names a registered profile
    global_default  no key, or a name that isn't registered -- ``None``

A profile never *replaces* a recipe. ``extra_bands`` are bands appended after
a type's own, so the base recipe (and every test pinned against it) is
untouched; a caller with no profile sees identical geometry to before this
module existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from . import fixture_types as ftypes
from .fixture_types import Band, MAT_BOXING


@dataclass(frozen=True)
class VisualProfile:
    """One named visual configuration. See module docstring for the model."""

    key: str
    # fixture_type_key -> bands appended after that type's own recipe.
    extra_bands: dict[str, tuple[Band, ...]] = dc_field(default_factory=dict)
    # band_name -> candidate asset keys, widening a single-asset band into a
    # small product catalog. A band whose own name isn't listed here keeps
    # using its own declared asset_key unchanged.
    product_catalog: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    # material_key -> RGBA override, applied on top of mesh_builder's own
    # palette. Empty by default -- most of this profile's visual identity
    # comes from geometry and product density, not new colours.
    material_overrides: dict[str, tuple[float, float, float, float]] = dc_field(
        default_factory=dict
    )
    # Named eye-level camera presets, merged into build_viewpoints()'s output
    # -- see mesh_builder.build_viewpoints. Each entry is resolved from the
    # layout's own bounds by mesh_builder, not a fixed coordinate, so the
    # same profile is portable across stores of different sizes.
    camera_presets: tuple[str, ...] = ()


VISUAL_PROFILES: dict[str, VisualProfile] = {}


def register(profile: VisualProfile) -> VisualProfile:
    VISUAL_PROFILES[profile.key] = profile
    return profile


def resolve_visual_profile(layout: Any) -> tuple[VisualProfile | None, str]:
    """
    R-VIS-01  meta.visual_profile names a registered profile   user_supplied
    R-VIS-05  unset, or names one that isn't registered        global_default

    An unrecognised name degrades to ``None`` (today's behaviour) rather than
    raising -- a typo in a project's meta should not take down model
    generation, the same failure-mode choice every other resolver here makes.
    """
    raw = (getattr(layout, "meta", None) or {}).get("visual_profile")
    if isinstance(raw, str) and raw in VISUAL_PROFILES:
        return VISUAL_PROFILES[raw], "user_supplied"
    return None, "global_default"


def effective_bands(spec: ftypes.FixtureType, profile: VisualProfile | None) -> tuple[Band, ...]:
    """A type's own bands, plus this profile's additions for that type (if
    any). Order matters only for z-fighting-free colours, not correctness --
    profile bands are appended, never inserted, so a type's own recipe always
    builds first."""
    if profile is None:
        return spec.bands
    return spec.bands + profile.extra_bands.get(spec.key, ())


def catalog_for_band(band: Band, profile: VisualProfile | None) -> tuple[str, ...] | None:
    """
    Resolve the set of candidate asset keys for a placed-asset band.

    Precedence: an explicit per-profile catalog entry for this exact band
    name, else the band's own declared ``asset_key`` (str or tuple), else
    None (no asset -- falls back to the procedural box, same as today).
    """
    if profile is not None and band.name in profile.product_catalog:
        return profile.product_catalog[band.name]
    if band.asset_key is None:
        return None
    if isinstance(band.asset_key, str):
        return (band.asset_key,)
    return band.asset_key


# ---------------------------------------------------------------------------
# Retail showroom profile
# ---------------------------------------------------------------------------
#
# Built against a client-supplied retail design reference (pages 2-8).
# Company-neutral in mechanism (nothing below is brand-specific code, only
# this data), and opt-in only -- see module docstring.

# Wall bays in the reference read as a continuous product zone framed by dark
# end pilasters -- a "wall bay" today has no edges wide enough for edge_w to
# read as anything but a sliver, so this is a project-specific styling choice,
# not a fact about the generic taxonomy. Spans 0.00-0.88 (base through the
# top of the product zone, stopping at the boxing band) so it reads as a
# continuous dark frame down both sides of the lighter product zone above the
# base cabinet. Procedural (no asset_key), so it is exactly as safe to
# Z-overlap the placed-asset stock bands as shelf_1-4 already are (see the
# Cycles containment note on wall_bay in fixture_types.py) -- narrow at the
# footprint edges besides, so it does not even reach the product grid's own
# cells; pinned by test_pilasters_do_not_bbox_contain_the_placed_phones_they_share_z_range_with.
_WALL_BAY_PILASTER = (
    Band("pilaster", 0.00, 0.88, MAT_BOXING, inset_d=0.15, back=True, edge_w=45.0),
)

# The reference's flagship tables mostly show phones with the occasional
# laptop/tablet in the mix (Views 2-4), not a uniform grid of one product --
# a product catalog widens island_table's "device" band beyond the single
# phone silhouette every project gets by default. Weighted toward phone (5:1)
# by simple repetition rather than a separate weight mechanism, matching the
# reference's own density: laptops are the occasional accent, not half the
# table. Only product_phone_generic and product_laptop_generic currently pass
# both the licence and visual-inspection bar (see assets/README.md at the
# repo root for the full search record, including rejected candidates for
# tablet/earbuds/smartwatch -- nothing cleared CC0 for those categories).
_PRODUCT_CATALOG = {
    "device": (
        "product_phone_generic", "product_phone_generic", "product_phone_generic",
        "product_phone_generic", "product_phone_generic", "product_laptop_generic",
    ),
}

RETAIL_SHOWROOM = register(
    VisualProfile(
        key="retail_showroom",
        extra_bands={"wall_bay": _WALL_BAY_PILASTER},
        product_catalog=_PRODUCT_CATALOG,
        camera_presets=("ENTRANCE", "ANGLED", "REAR"),
        # No lighting hint: the photoreal ceiling downlight grid (see
        # render_scene.py) is a generic default for every project, not a
        # per-profile styling choice -- a lit-ceiling look is a defensible
        # improvement for any retail-style room, not something specific to
        # this reference, so it needed no profile-specific tuning.
    )
)
