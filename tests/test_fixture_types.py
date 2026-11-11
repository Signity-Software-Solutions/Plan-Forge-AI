"""Fixture type resolution across both key spaces, and recipe sanity."""

import pytest

from app.services.fixture_types import (
    FALLBACK_TYPE,
    FIXTURE_TYPES,
    get_type,
    resolve_fixture_type,
)


# ---------------------------------------------------------------------------
# Taxonomy sanity — these guard the table itself, not the matcher
# ---------------------------------------------------------------------------


def test_every_type_declares_positive_defaults():
    for key, spec in FIXTURE_TYPES.items():
        assert spec.key == key, f"{key} key mismatch"
        assert spec.default_w > 0 and spec.default_d > 0 and spec.default_h > 0, key
        assert spec.default_elevation >= 0, key


def test_every_type_has_at_least_one_band():
    for key, spec in FIXTURE_TYPES.items():
        assert spec.bands, key


def test_bands_are_ordered_within_the_unit_height():
    for key, spec in FIXTURE_TYPES.items():
        for band in spec.bands:
            assert 0.0 <= band.z0 < band.z1 <= 1.0, f"{key}.{band.name}"


def test_bands_never_widen_the_footprint():
    """
    Width still only ever shrinks. A band wider than its fixture would push the
    model's X/Y bounds past the layout's for no visual gain, and the overhang
    read comes from insetting the body beneath instead.
    """
    for key, spec in FIXTURE_TYPES.items():
        for band in spec.bands:
            assert 0.0 <= band.inset_w < 1.0, f"{key}.{band.name} inset_w"
            assert 0.0 <= band.inset_d < 1.0, f"{key}.{band.name} inset_d"


def test_depth_projection_stays_inside_the_types_declared_envelope():
    """
    Depth is the one axis a band may grow along, and the envelope is what keeps
    that honest. This is the recipe-level half of the rule -- it catches a
    recipe asking for more room than its type admits to, at import time, rather
    than letting mesh_builder silently clamp it later.
    """
    for key, spec in FIXTURE_TYPES.items():
        assert spec.poc_envelope_d >= 1.0, f"{key} envelope shrinks the footprint"
        for band in spec.bands:
            assert band.project_d >= 0.0, f"{key}.{band.name} projects backwards"
            occupied = (1.0 - band.inset_d) + band.project_d
            assert occupied <= spec.poc_envelope_d + 1e-9, (
                f"{key}.{band.name} wants {occupied:.2f}x depth, "
                f"envelope allows {spec.poc_envelope_d:.2f}x"
            )


def test_only_wall_mounted_shelving_projects_at_all():
    """
    Projection is a POC allowance for one specific problem -- a BOQ that states
    a wall bay's panel depth rather than its shelved depth. It is not a licence
    for any recipe to spread, so the set that uses it is pinned here.
    """
    projecting = {
        key for key, spec in FIXTURE_TYPES.items()
        if any(b.project_d > 0.0 for b in spec.bands)
    }
    assert projecting == {"wall_bay"}
    # Everything else must still be exactly footprint-bounded.
    for key, spec in FIXTURE_TYPES.items():
        if key not in projecting:
            assert spec.poc_envelope_d == 1.0, f"{key} widened its envelope without using it"


def test_types_with_real_assets_still_have_a_parametric_fallback():
    """
    Every needs_asset type must still resolve on the band-recipe path -- the asset
    is an enhancement fit_asset can refuse, never a hard requirement, per the
    refuse-rather-than-distort rule.
    """
    needing = {k for k, s in FIXTURE_TYPES.items() if s.needs_asset}
    assert needing == {"chair", "stool", "pillar"}
    for key in needing:
        assert FIXTURE_TYPES[key].bands, f"{key} has no parametric fallback"


def test_item_codes_are_unique_across_types():
    seen: dict[str, str] = {}
    for key, spec in FIXTURE_TYPES.items():
        for code in spec.item_codes:
            token = code.upper()
            assert token not in seen, f"{token} claimed by {seen.get(token)} and {key}"
            seen[token] = key


def test_get_type_falls_back_rather_than_raising():
    assert get_type("no_such_type").key == FALLBACK_TYPE


# ---------------------------------------------------------------------------
# Key space 1 — BOQ workbooks, where label == kind == item code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("13-A", "base_cabinet"),
        ("12-L", "wall_bay"),
        ("12-P", "display_panel"),
        ("14-A", "screen_wall"),
        ("14-B", "boxing"),
        ("9-B", "service_desk"),
        ("21-B", "pillar"),
    ],
)
def test_boq_item_code_resolves_by_exact_match(code, expected):
    key, source, rule = resolve_fixture_type(code, code)
    assert key == expected
    assert source == "parsed"
    assert rule == "R-FXT-01"


def test_item_code_matching_is_case_and_space_insensitive():
    assert resolve_fixture_type(" 13-a ", " 13-a ")[0] == "base_cabinet"


# ---------------------------------------------------------------------------
# Key space 2 — plan sources, where label is a description
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("BASE CABINET (BC-1.1M)", "base_cabinet"),
        ("BASE WALL-1 (BW-1)", "wall_bay"),
        ("FLAT PANEL-7 (FP-7)", "display_panel"),
        ("ACCESSORY FLAT PANEL-1 (NP-1)", "display_panel"),
        ("CENTER BAR NARROW - NAR-5M-END", "island_table"),
        ("TRAINING TABLE-2P", "island_table"),
        ("CUSTOMER SERVICE VENEERED-2P (CS-2P-V)", "service_desk"),
        # A "bar chair" is a tall counter seat — a stool. The sample BOQ lists
        # them at 600-750mm, which is stool height, not chair height.
        ("BAR CHAIR - TRAINING TABLE", "stool"),
        ("WOODEN STOOL", "stool"),
        ("BRAND WALL (LFD)-2100", "screen_wall"),
        ("BW-TOP BOXING-2100", "boxing"),
        ("WALL PANELING LAM.", "partition"),
    ],
)
def test_catalogue_descriptions_resolve_by_keyword(label, expected):
    key, source, rule = resolve_fixture_type(label, "fixture")
    assert key == expected, f"{label} -> {key}"
    assert rule in ("R-FXT-01", "R-FXT-02")
    assert source == "parsed"


def test_keyword_matching_is_case_insensitive():
    assert resolve_fixture_type("base cabinet lower", "")[0] == "base_cabinet"
    assert resolve_fixture_type("BASE CABINET LOWER", "")[0] == "base_cabinet"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("display", "display_panel"),
        ("table", "island_table"),
        ("counter", "counter"),
        ("shelving", "shelf_unit"),
        ("chair", "chair"),
    ],
)
def test_plan_category_vocabulary_resolves_by_kind(kind, expected):
    key, source, rule = resolve_fixture_type("Some Label", kind)
    assert key == expected
    assert rule == "R-FXT-03"
    assert source == "parsed"


def test_generic_kinds_carry_no_information():
    """"generic"/"fixture" are parser defaults, not categories — must not match."""
    for kind in ("generic", "fixture", ""):
        key, _, rule = resolve_fixture_type("", kind)
        assert key == FALLBACK_TYPE
        assert rule == "R-FXT-05"


# ---------------------------------------------------------------------------
# Key space 3 — nothing identifies it, so fall back to shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "w,d,h,expected",
    [
        (1100, 100, 2200, "wall_bay"),       # tall and thin against a wall
        (1040, 150, 600, "display_panel"),   # thin, waist-to-eye height
        (1400, 900, 900, "island_table"),    # waist-high and deep
        (3000, 900, 950, "counter"),         # same but long
        (950, 30, 200, "signage_panel"),     # small and flat
    ],
)
def test_dimensional_heuristic_is_the_last_resort(w, d, h, expected):
    key, source, rule = resolve_fixture_type("", "", w, d, h)
    assert key == expected
    assert rule == "R-FXT-04"
    # Derived, not parsed — a guess from shape ranks below a stated identity.
    assert source == "derived"


def test_heuristic_declines_rather_than_forcing_a_type():
    key, _, rule = resolve_fixture_type("", "", 500, 500, 500)
    assert key == FALLBACK_TYPE
    assert rule == "R-FXT-05"


def test_heuristic_ignores_missing_or_zero_dimensions():
    assert resolve_fixture_type("", "", None, None, None)[0] == FALLBACK_TYPE
    assert resolve_fixture_type("", "", 0, 0, 0)[0] == FALLBACK_TYPE


# ---------------------------------------------------------------------------
# Cascade precedence
# ---------------------------------------------------------------------------


def test_item_code_beats_keyword_beats_kind_beats_shape():
    # Item code wins even when the description says something else.
    assert resolve_fixture_type("13-A", "13-A")[2] == "R-FXT-01"
    # Keyword wins over the kind vocabulary.
    assert resolve_fixture_type("BASE CABINET", "display")[0] == "base_cabinet"
    # Kind wins over shape.
    key, _, rule = resolve_fixture_type("", "counter", 1100, 100, 2200)
    assert key == "counter" and rule == "R-FXT-03"


def test_resolution_never_raises_on_junk_input():
    for label, kind in ((None, None), ("", ""), ("\\n\\t", "  "), ("x" * 500, "y" * 500)):
        key, source, rule = resolve_fixture_type(label, kind)
        assert key in FIXTURE_TYPES
        assert rule.startswith("R-FXT-")
        assert source


def test_resolution_is_deterministic():
    """Regenerate-on-demand depends on this all the way down."""
    args = ("BASE WALL-1 (BW-1)", "fixture", 1100, 100, 2193)
    assert resolve_fixture_type(*args) == resolve_fixture_type(*args)


def test_bar_chair_resolves_to_stool_not_chair():
    """
    Seating is matched before the furniture it stands at, and a bar chair is a
    stool. Getting this wrong cost an exact asset fit: the BOQ's 750mm bar chair
    matches a 750mm stool asset precisely, but a 1006mm chair asset only via a
    20% rescale.
    """
    for label in ("BAR CHAIR - TRAINING TABLE", "BAR CHAIR - CUSTOMER SERVICE"):
        assert resolve_fixture_type(label, "")[0] == "stool", label
    assert resolve_fixture_type("Chair (Cashier area)", "")[0] == "chair"


def test_seating_declares_seat_height_semantics():
    """
    Retail fixture schedules quote seat height for seating and overall height for
    everything else. The type has to say which, or an asset fit compares two
    different measurements.
    """
    for key in ("chair", "stool"):
        assert FIXTURE_TYPES[key].height_means == "seat", key
    for key in ("base_cabinet", "island_table", "counter", "wall_bay"):
        assert FIXTURE_TYPES[key].height_means == "overall", key


def test_light_box_resolves_by_keyword_and_kind():
    for label in ("ISB Light Box", "LFD Light Box", "lightbox"):
        assert resolve_fixture_type(label, "")[0] == "light_box", label
    assert resolve_fixture_type("", "light_box")[0] == "light_box"


def test_light_box_has_an_emissive_face_band():
    from app.services.fixture_types import MAT_EMISSIVE
    spec = FIXTURE_TYPES["light_box"]
    materials = {b.material for b in spec.bands}
    assert MAT_EMISSIVE in materials


def test_light_box_does_not_collide_with_boxing_keyword():
    """"boxing" is also a boxing-type keyword; light_box's own keywords must not
    accidentally match generic top-boxing items, and vice versa."""
    assert resolve_fixture_type("BW-TOP BOXING-2100", "")[0] == "boxing"
    assert resolve_fixture_type("ISB LIGHT BOX", "")[0] == "light_box"


# ---------------------------------------------------------------------------
# Populated recipes — every reference fixture must read as an assembly
# ---------------------------------------------------------------------------


def test_repeat_counts_are_sane():
    for key, spec in FIXTURE_TYPES.items():
        for band in spec.bands:
            assert band.repeat_w >= 1, f"{key}.{band.name} repeat_w"
            assert band.repeat_d >= 1, f"{key}.{band.name} repeat_d"
            if band.repeat_w > 1 or band.repeat_d > 1:
                # A copy with no inset exactly fills its cell and fuses with its
                # neighbours, which defeats the point of repeating it at all.
                assert band.inset_w > 0.0 or band.inset_d > 0.0, (
                    f"{key}.{band.name} repeats without an inset, so copies touch"
                )


def test_the_fixtures_that_carry_product_actually_declare_some():
    """
    The showroom read as empty because nothing carried product. These are the
    types a customer looks at, so each must declare at least one repeated band.
    """
    for key in ("island_table", "wall_bay", "shelf_unit"):
        spec = FIXTURE_TYPES[key]
        assert any(b.repeat_w > 1 or b.repeat_d > 1 for b in spec.bands), key


def test_headline_fixtures_are_assemblies_not_boxes():
    """A single-band recipe for any of these is the bug this work fixed."""
    for key in ("island_table", "wall_bay", "base_cabinet", "service_desk", "shelf_unit"):
        assert len(FIXTURE_TYPES[key].bands) >= 4, f"{key} is still nearly a box"


def test_an_accessory_table_is_a_table_not_a_shelving_unit():
    """
    It sits in the middle of the floor in the reference. Typed as shelf_unit it
    rendered as a bookcase standing in open floor -- "accessor" is a shelf_unit
    keyword and was catching it before island_table could.
    """
    assert resolve_fixture_type("Accessory Table", "fixture")[0] == "island_table"
    # The keyword that caused it must still work for actual shelving.
    assert resolve_fixture_type("Accessory Wall Shelf", "fixture")[0] == "shelf_unit"
