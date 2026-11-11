"""Asset normalisation, fitting and the refuse-rather-than-distort rule."""

import numpy as np
import pytest
import trimesh

from app.services import asset_loader
from app.services.asset_loader import (
    FIT_EXACT,
    FIT_UNIFORM,
    available_assets,
    fit_asset,
    load_asset,
    place,
)

_REQUIRED_MANIFEST_KEYS = {
    "file", "native_units", "native_up_axis", "licence", "source", "author",
}


# ---------------------------------------------------------------------------
# Manifest — these files ship inside client deliverables
# ---------------------------------------------------------------------------


def test_every_asset_declares_licence_and_provenance():
    """
    An attribution obligation discovered inside a client proposal is expensive,
    so licence and source are mandatory metadata, not documentation.
    """
    assets = available_assets()
    assert assets, "expected at least one asset"
    for key, entry in assets.items():
        missing = _REQUIRED_MANIFEST_KEYS - set(entry)
        assert not missing, f"{key} missing {sorted(missing)}"


def test_all_assets_are_cc0():
    for key, entry in available_assets().items():
        assert entry["licence"] == "CC0-1.0", f"{key} is {entry['licence']}"


def test_manifest_declares_units_and_up_axis():
    """Without these, normalisation is guesswork."""
    for key, entry in available_assets().items():
        assert entry["native_units"] in {"mm", "cm", "m", "in", "ft"}, key
        assert entry["native_up_axis"] in {"Y", "Z"}, key


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(available_assets()))
def test_assets_normalise_to_millimetres_z_up_on_the_floor(key):
    mesh = load_asset(key)
    assert mesh is not None, key
    lo, hi = mesh.bounds
    extents = hi - lo

    # Underside sits on the floor plane.
    assert lo[2] == pytest.approx(0.0, abs=1e-6), f"{key} does not sit on z=0"
    # Footprint is centred on the origin, so placement is a plain translation.
    assert (lo[0] + hi[0]) / 2 == pytest.approx(0.0, abs=1e-6)
    assert (lo[1] + hi[1]) / 2 == pytest.approx(0.0, abs=1e-6)
    # Millimetres, not metres: furniture is hundreds of mm tall, and even a
    # small product prop (a phone, stood in its native orientation) is tens
    # of mm, never a fraction of a mm.
    assert 50.0 < extents[2] < 3000.0, f"{key} height {extents[2]} looks like metres"
    # Z-up: seating and small standing props alike are taller than they are
    # wide in their native orientation (a placed prop may later be tipped
    # flat at placement time -- see mesh_builder._place_prop_in_cell -- but
    # the asset as normalised here always stands) -- EXCEPT an asset declared
    # native_pose="resting" (see asset_loader.native_pose), which has no
    # sensible standing pose to begin with (an open laptop is an L-shape, not
    # a slab) and is normalised already in its natural table pose instead.
    if asset_loader.native_pose(key) == "standing":
        assert extents[2] > extents[0] * 0.8, f"{key} looks like it is still Y-up"


def test_missing_asset_returns_none_rather_than_raising():
    """None is a first-class outcome — the caller falls back to the recipe."""
    assert load_asset("no_such_fixture_type") is None


def test_loading_is_cached():
    a = load_asset("stool")
    b = load_asset("stool")
    assert a is b, "expected the same cached object"


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _native_height(key: str) -> float:
    mesh = load_asset(key)
    return float((mesh.bounds[1] - mesh.bounds[0])[2])


def test_matching_height_is_an_exact_fit_with_no_scaling():
    h = _native_height("stool")
    fit = fit_asset("stool", 400, 400, h)
    assert fit is not None
    assert fit.method == FIT_EXACT
    assert fit.scale == 1.0
    assert fit.is_exact


def test_the_boq_bar_chair_height_is_an_exact_fit():
    """
    The sample BOQ lists BAR CHAIR at 750mm and the stool asset is authored at
    750.5mm — inside the 1% exact band, so it is placed untouched.
    """
    fit = fit_asset("stool", 400, 400, 750)
    assert fit is not None
    assert fit.method == FIT_EXACT


def test_moderate_difference_scales_uniformly():
    h = _native_height("chair")
    fit = fit_asset("chair", 450, 450, h * 0.85)
    assert fit is not None
    assert fit.method == FIT_UNIFORM
    assert fit.scale == pytest.approx(0.85, abs=1e-3)


def test_uniform_scaling_preserves_proportions():
    """
    The whole point of scaling uniformly: a squashed chair reads as broken, while
    a slightly-wrong-sized one reads as schematic.
    """
    mesh = load_asset("chair")
    before = mesh.bounds[1] - mesh.bounds[0]
    fit = fit_asset("chair", 450, 450, before[2] * 0.8)
    after = fit.mesh.bounds[1] - fit.mesh.bounds[0]
    assert (after / before) == pytest.approx([0.8, 0.8, 0.8], abs=1e-6)


def test_extreme_difference_is_refused_rather_than_distorted():
    """
    A 3m 'chair' is not a chair. Refusing hands the caller back to the parametric
    recipe, which is correct at any size by construction.
    """
    assert fit_asset("stool", 400, 400, 3000) is None
    assert fit_asset("stool", 400, 400, 150) is None


def test_fit_declines_for_types_with_no_asset():
    assert fit_asset("base_cabinet", 1100, 320, 900) is None


def test_fit_declines_on_nonsense_height():
    assert fit_asset("stool", 400, 400, 0) is None
    assert fit_asset("stool", 400, 400, -100) is None


def test_fitting_does_not_mutate_the_cached_mesh():
    """The cache is shared across requests; a fit that mutated it would poison it."""
    before = load_asset("chair").bounds.copy()
    fit_asset("chair", 450, 450, _native_height("chair") * 0.7)
    assert load_asset("chair").bounds == pytest.approx(before)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_place_translates_and_rotates_about_z():
    mesh = load_asset("stool")
    placed = place(mesh, 1000.0, 2000.0, 0.0, 0.0)
    lo, hi = placed.bounds
    assert (lo[0] + hi[0]) / 2 == pytest.approx(1000.0, abs=1e-6)
    assert (lo[1] + hi[1]) / 2 == pytest.approx(2000.0, abs=1e-6)
    assert lo[2] == pytest.approx(0.0, abs=1e-6)


def test_place_honours_elevation():
    placed = place(load_asset("stool"), 0.0, 0.0, 500.0, 0.0)
    assert placed.bounds[0][2] == pytest.approx(500.0, abs=1e-6)


def test_place_leaves_the_source_untouched():
    mesh = load_asset("stool")
    before = mesh.bounds.copy()
    place(mesh, 5000.0, 5000.0, 0.0, 1.2)
    assert mesh.bounds == pytest.approx(before)


def test_rotation_preserves_height():
    mesh = load_asset("chair")
    h = (mesh.bounds[1] - mesh.bounds[0])[2]
    placed = place(mesh, 0.0, 0.0, 0.0, np.pi / 3)
    assert (placed.bounds[1] - placed.bounds[0])[2] == pytest.approx(h, abs=1e-6)


# ---------------------------------------------------------------------------
# Height semantics — seat height vs overall height
# ---------------------------------------------------------------------------


def test_seating_assets_declare_a_seat_height():
    """
    Without it, a declared seat height is compared against an overall height —
    two different measurements — and a good asset is rejected.
    """
    for key in ("chair", "stool"):
        entry = available_assets()[key]
        assert "seat_height_mm" in entry, key
        assert 0 < entry["seat_height_mm"] < entry["native_height_mm"], key


def test_seat_height_is_interpreted_as_seat_height():
    """
    The bug this pins: retail schedules quote seat height for seating. A 450mm
    chair in a fixture schedule is a 450mm *seat*, not a 450mm chair. Measured
    against the asset's overall 1007mm that is a 55% mismatch and gets refused;
    measured against its 524mm seat it fits comfortably.
    """
    seat = available_assets()["chair"]["seat_height_mm"]

    as_overall = fit_asset("chair", 450, 450, 450, height_means="overall")
    assert as_overall is None, "450mm as an overall height should be refused"

    as_seat = fit_asset("chair", 450, 450, 450, height_means="seat")
    assert as_seat is not None, "450mm as a seat height should fit"
    assert as_seat.scale == pytest.approx(450 / seat, abs=1e-3)


def test_seat_interpretation_scales_the_whole_object():
    """A chair with a 450mm seat is ~865mm overall, not 450mm tall."""
    fit = fit_asset("chair", 450, 450, 450, height_means="seat")
    overall = float((fit.mesh.bounds[1] - fit.mesh.bounds[0])[2])
    assert overall > 700, f"expected a full-height chair, got {overall:.0f}mm"


def test_boq_bar_chair_seat_heights_both_fit():
    """The sample BOQ lists bar chairs at 600mm and 750mm — both seat heights."""
    for h in (600, 750):
        assert fit_asset("stool", 400, 400, h, height_means="seat") is not None, h


def test_overall_is_still_the_default():
    """Only types that say otherwise get seat interpretation."""
    seat = available_assets()["stool"]["seat_height_mm"]
    overall = available_assets()["stool"]["native_height_mm"]
    assert seat != overall
    fit = fit_asset("stool", 400, 400, overall)
    assert fit is not None and fit.method == FIT_EXACT


# ---------------------------------------------------------------------------
# Footprint plausibility — a uniform scale can still be a bad fit
# ---------------------------------------------------------------------------


def test_footprint_is_checked_not_just_height(monkeypatch):
    """
    The bug this pins: a search for more CC0 shelf assets turned up one authored
    at 372x1078x1556mm. Scaled uniformly to a 2100mm shelf height (+35%, right at
    the limit) it passes the height check alone -- but the resulting 502x1455mm
    footprint is 4x deeper than the 350mm a shelf fixture actually needs. Height
    tolerance was never enough on its own; the footprint has to be checked too.
    """
    box = trimesh.creation.box(extents=(372.0, 1078.0, 1556.0))
    box.apply_translation((0.0, 0.0, 778.0))
    monkeypatch.setitem(asset_loader._mesh_cache, "_narrow_deep_shelf", box)

    fit = fit_asset("_narrow_deep_shelf", 1000.0, 350.0, 2100.0)
    assert fit is None, "a 4x-deep footprint must be refused, not silently scaled"


def test_footprint_check_is_skipped_when_no_footprint_was_declared():
    """Seating fits by height alone; the BOQ often carries no chair footprint."""
    fit = fit_asset("chair", 0.0, 0.0, _native_height("chair") * 0.85)
    assert fit is not None


def test_reasonably_proportioned_asset_still_fits(monkeypatch):
    """The footprint check should not be so strict it rejects a good match."""
    box = trimesh.creation.box(extents=(1000.0, 350.0, 2000.0))
    box.apply_translation((0.0, 0.0, 1000.0))
    monkeypatch.setitem(asset_loader._mesh_cache, "_well_proportioned_shelf", box)

    fit = fit_asset("_well_proportioned_shelf", 1000.0, 350.0, 2100.0)
    assert fit is not None
    assert fit.method == FIT_UNIFORM


# ---------------------------------------------------------------------------
# Pillar — the second real asset, "overall" height semantics (not seat height)
# ---------------------------------------------------------------------------


def test_pillar_asset_is_present_and_normalises_correctly():
    mesh = load_asset("pillar")
    assert mesh is not None
    lo, hi = mesh.bounds
    assert lo[2] == pytest.approx(0.0, abs=1e-6), "pillar does not sit on the floor"
    assert (lo[0] + hi[0]) / 2 == pytest.approx(0.0, abs=1e-6)
    assert (lo[1] + hi[1]) / 2 == pytest.approx(0.0, abs=1e-6)
    assert (hi - lo)[2] > 1000, "height looks like it is still in metres, not mm"


def test_pillar_fits_exactly_at_its_native_size():
    mesh = load_asset("pillar")
    native = (mesh.bounds[1] - mesh.bounds[0])
    fit = fit_asset("pillar", native[0], native[1], native[2])
    assert fit is not None and fit.method == FIT_EXACT


def test_pillar_is_refused_at_this_taxonomy_types_own_default_dimensions():
    """
    The bug class this pins: a real asset can pass licence and rough-dimension
    checks and still be the wrong shape for how a type's own defaults describe it.
    The pillar type defaults to a 300x300mm footprint at 3000mm tall; the asset is
    a 400x400x2400mm column, and uniform-scaling it to 3000mm tall produces a
    500x500mm footprint -- 67% wider than 300mm, well past the refusal tolerance.
    This must refuse, not silently ship a fat pillar.
    """
    from app.services.fixture_types import get_type

    spec = get_type("pillar")
    fit = fit_asset("pillar", spec.default_w, spec.default_d, spec.default_h)
    assert fit is None


def test_pillar_fits_when_footprint_is_proportioned_to_the_asset():
    """The refusal above is about proportion, not the asset being unusable."""
    fit = fit_asset("pillar", 450, 450, 2700)
    assert fit is not None
    assert fit.method == FIT_UNIFORM
