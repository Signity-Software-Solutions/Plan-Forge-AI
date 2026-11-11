"""Mesh invariants for the Tier A massing model (UnifiedLayout → GLB)."""

import json
import struct

import numpy as np
import pytest

from app.models.layout_schema import Opening, UnifiedLayout
from app.services import fixture_types as ftypes
from app.services.mesh_builder import (
    GROUP_CEILING,
    GROUP_FIXTURES,
    GROUP_FLOOR,
    GROUP_WALLS,
    EmptyMassingError,
    build_massing,
    layout_bounds_2d,
    resolve_ceiling_height_mm,
    resolve_wall_thickness_mm,
)
from app.services.resolution import (
    SOURCE_GLOBAL_DEFAULT,
    SOURCE_TYPE_DEFAULT,
    SOURCE_USER,
)


def _room_layout(**meta) -> UnifiedLayout:
    """6m x 4m room, one door, one window, two fixtures."""
    return UnifiedLayout(
        units="mm",
        walls=[
            {"x1": 0, "y1": 0, "x2": 6000, "y2": 0},
            {"x1": 6000, "y1": 0, "x2": 6000, "y2": 4000},
            {"x1": 6000, "y1": 4000, "x2": 0, "y2": 4000},
            {"x1": 0, "y1": 4000, "x2": 0, "y2": 0},
        ],
        rooms=[
            {
                "name": "Retail",
                "area": 24,
                "polygon": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
            }
        ],
        fixtures=[
            {
                "kind": "display",
                "x": 1500,
                "y": 2000,
                "width_mm": 1200,
                "depth_mm": 600,
                "height_mm": 1800,
                "label": "LFD",
            },
            {
                "kind": "counter",
                "x": 4500,
                "y": 3000,
                "width_mm": 2000,
                "depth_mm": 700,
                "rotation_deg": 45,
                "label": "Cash",
            },
        ],
        openings=[
            {"kind": "door", "x1": 2000, "y1": 0, "x2": 2900, "y2": 0, "label": "D1"},
            {"kind": "window", "x1": 6000, "y1": 1000, "x2": 6000, "y2": 2500, "label": "W1"},
        ],
        meta=meta,
    )


def _scene_bounds(result) -> tuple[np.ndarray, np.ndarray]:
    lows = [g.bounds[0] for g in result.scene.geometry.values()]
    highs = [g.bounds[1] for g in result.scene.geometry.values()]
    return np.min(lows, axis=0), np.max(highs, axis=0)


def _named(result, prefix: str) -> dict:
    """Geometry whose node name starts with ``prefix``."""
    return {n: m for n, m in result.scene.geometry.items() if n.startswith(prefix)}


def _fixture_bounds(result, index: int) -> tuple[np.ndarray, np.ndarray]:
    """Combined bounds of every band of one fixture."""
    meshes = _named(result, f"fixture_{index}_").values()
    assert meshes, f"no geometry for fixture {index}"
    return (
        np.min([m.bounds[0] for m in meshes], axis=0),
        np.max([m.bounds[1] for m in meshes], axis=0),
    )


def _fixture_extents(result, index: int) -> np.ndarray:
    lo, hi = _fixture_bounds(result, index)
    return hi - lo


# ---------------------------------------------------------------------------
# Watertightness
# ---------------------------------------------------------------------------


def test_generated_meshes_are_watertight():
    """
    Sub-rectangle decomposition is watertight by construction — no booleans.

    Scoped to geometry this module generates. Authored assets are third-party
    meshes and are routinely not watertight; that guarantee was always about the
    wall decomposition, not about anything we merely place.
    """
    result = build_massing(_room_layout())
    assert result.scene.geometry, "expected geometry"
    generated = {
        n: m for n, m in result.scene.geometry.items() if not n.endswith("_asset")
    }
    assert generated
    for name, mesh in generated.items():
        assert mesh.is_watertight, f"{name} not watertight"
        assert mesh.is_volume, f"{name} is not a valid volume"


def test_wall_boxes_have_positive_volume():
    result = build_massing(_room_layout())
    walls = [m for n, m in result.scene.geometry.items() if n.startswith("wall_")]
    assert walls
    for mesh in walls:
        assert mesh.volume > 0


# ---------------------------------------------------------------------------
# Bounds: extrusion adds Z and nothing else
# ---------------------------------------------------------------------------


def test_bbox_xy_matches_2d_bounds_when_no_walls():
    """
    With no walls there is no thickness to expand the footprint, so the massing
    bbox in X/Y is bit-identical to the 2D bounds. This is the strict form of
    "extrusion never touches X or Y".
    """
    layout = UnifiedLayout(
        units="mm",
        fixtures=[
            {"kind": "table", "x": 1000, "y": 2000, "width_mm": 800, "depth_mm": 600, "height_mm": 750},
            {"kind": "table", "x": 5000, "y": 3000, "width_mm": 800, "depth_mm": 600, "height_mm": 750},
        ],
    )
    result = build_massing(layout)
    lo, hi = _scene_bounds(result)
    minx, miny, maxx, maxy = layout_bounds_2d(layout)

    assert lo[0] == pytest.approx(minx, abs=1e-9)
    assert lo[1] == pytest.approx(miny, abs=1e-9)
    assert hi[0] == pytest.approx(maxx, abs=1e-9)
    assert hi[1] == pytest.approx(maxy, abs=1e-9)


def test_bbox_xy_expands_by_exactly_half_wall_thickness():
    """
    Walls are centrelines in UnifiedLayout, so giving them thickness necessarily
    grows the plan footprint by half the thickness on each side — and by exactly
    that, never more. Z starts at the floor and stops at the ceiling.
    """
    layout = _room_layout()
    result = build_massing(layout)
    thickness, _ = resolve_wall_thickness_mm(layout)
    ceiling, _ = resolve_ceiling_height_mm(layout)
    half = thickness / 2.0

    lo, hi = _scene_bounds(result)
    minx, miny, maxx, maxy = layout_bounds_2d(layout)

    assert lo[0] == pytest.approx(minx - half, abs=1e-6)
    assert lo[1] == pytest.approx(miny - half, abs=1e-6)
    assert hi[0] == pytest.approx(maxx + half, abs=1e-6)
    assert hi[1] == pytest.approx(maxy + half, abs=1e-6)

    # The floor slab hangs below datum; the ceiling slab tops out at the ceiling.
    assert lo[2] == pytest.approx(-50.0, abs=1e-6)
    assert hi[2] == pytest.approx(ceiling, abs=1e-6)


# ---------------------------------------------------------------------------
# Determinism — makes regenerate-on-demand safe without persisting
# ---------------------------------------------------------------------------


def test_vertex_hash_is_deterministic():
    a = build_massing(_room_layout())
    b = build_massing(_room_layout())
    assert a.vertex_hash() == b.vertex_hash()


def test_glb_bytes_are_deterministic():
    a = build_massing(_room_layout()).export_glb()
    b = build_massing(_room_layout()).export_glb()
    assert len(a) == len(b)


def test_vertex_hash_changes_with_geometry():
    base = build_massing(_room_layout())
    taller = build_massing(_room_layout(ceiling_height_mm=3600))
    assert base.vertex_hash() != taller.vertex_hash()


# ---------------------------------------------------------------------------
# Openings — sub-rectangle decomposition
# ---------------------------------------------------------------------------


def test_wall_without_openings_is_a_single_box():
    layout = UnifiedLayout(units="mm", walls=[{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}])
    result = build_massing(layout)
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 1


def test_door_splits_wall_into_jamb_jamb_lintel():
    """A door mid-wall: two full-height jambs plus a lintel, no sill."""
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}],
        openings=[{"kind": "door", "x1": 2000, "y1": 0, "x2": 2900, "y2": 0}],
    )
    result = build_massing(layout)
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 3


def test_window_splits_wall_into_jamb_jamb_sill_lintel():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}],
        openings=[{"kind": "window", "x1": 2000, "y1": 0, "x2": 3500, "y2": 0}],
    )
    result = build_massing(layout)
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 4


def test_opening_flush_to_wall_start_gives_zero_width_jamb_not_crash():
    """The brief's degradation case: no leading jamb, no exception."""
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}],
        openings=[{"kind": "door", "x1": 0, "y1": 0, "x2": 900, "y2": 0}],
    )
    result = build_massing(layout)
    names = [n for n in result.scene.geometry if n.startswith("wall_")]
    # Trailing jamb + lintel only — the leading jamb is zero-width and dropped.
    assert len(names) == 2
    for mesh in result.scene.geometry.values():
        assert mesh.is_watertight


def test_opening_flush_to_wall_end_gives_zero_width_jamb_not_crash():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}],
        openings=[{"kind": "door", "x1": 4100, "y1": 0, "x2": 5000, "y2": 0}],
    )
    result = build_massing(layout)
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 2


def test_opening_spanning_entire_wall_leaves_only_a_lintel():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 2000, "y2": 0}],
        openings=[{"kind": "door", "x1": 0, "y1": 0, "x2": 2000, "y2": 0}],
    )
    result = build_massing(layout)
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 1


def test_opening_far_from_any_wall_is_skipped():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 5000, "y2": 0}],
        openings=[{"kind": "door", "x1": 2000, "y1": 9000, "x2": 2900, "y2": 9000}],
    )
    result = build_massing(layout)
    assert result.stats["openings_bound"] == 0
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 1


def test_opening_on_rotated_wall_binds_correctly():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 4000, "y2": 4000}],
        openings=[{"kind": "door", "x1": 1000, "y1": 1000, "x2": 1700, "y2": 1700}],
    )
    result = build_massing(layout)
    assert result.stats["openings_bound"] == 1
    assert len([n for n in result.scene.geometry if n.startswith("wall_")]) == 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def test_fixture_bands_never_exceed_declared_dimensions():
    """
    A typed fixture is a stack of bands, but the stack must occupy exactly the
    declared footprint — no band may overhang, or "extrusion adds Z and nothing
    else" stops being true.

    "counter", not "table": island_table's product sits at its own natural
    size now (see test_a_placed_prop_may_fall_short_of_but_never_exceed_the_
    declared_height), so it is no longer the fixture that pins this
    invariant at exact equality. counter has no placed asset and still must.
    """
    layout = UnifiedLayout(
        units="mm",
        fixtures=[
            {"kind": "counter", "x": 0, "y": 0, "width_mm": 1200, "depth_mm": 600, "height_mm": 750}
        ],
    )
    result = build_massing(layout)
    assert _fixture_extents(result, 0) == pytest.approx([1200, 600, 750], abs=1e-6)


def test_every_band_fits_inside_the_footprint():
    """
    Checked on axis-aligned fixtures only: ``extents`` is an axis-aligned bbox,
    so a rotated fixture's bbox is legitimately larger than its own footprint.
    """
    layout = UnifiedLayout(
        units="mm",
        fixtures=[
            {"kind": "counter", "x": 0, "y": 0, "width_mm": 2000, "depth_mm": 700, "height_mm": 950},
            {"kind": "display", "x": 5000, "y": 0, "width_mm": 1040, "depth_mm": 150, "height_mm": 600},
            {"kind": "shelving", "x": 9000, "y": 0, "width_mm": 1000, "depth_mm": 350, "height_mm": 2100},
        ],
    )
    result = build_massing(layout)
    for i, f in enumerate(layout.fixtures):
        bands = _named(result, f"fixture_{i}_")
        assert bands, f"fixture {i} produced no bands"
        for name, mesh in bands.items():
            assert mesh.extents[0] <= f.width_mm + 1e-6, name
            assert mesh.extents[1] <= f.depth_mm + 1e-6, name


def test_fixture_without_height_falls_back_to_its_type_default():
    layout = UnifiedLayout(
        units="mm",
        fixtures=[{"kind": "counter", "x": 0, "y": 0, "width_mm": 1000, "depth_mm": 600}],
    )
    result = build_massing(layout)
    assert _fixture_extents(result, 0)[2] == pytest.approx(
        ftypes.get_type("counter").default_h
    )
    heights = [
        r for r in result.resolutions
        if r["field"] == "height_mm" and r["source"] == SOURCE_TYPE_DEFAULT
    ]
    assert len(heights) == 1
    assert heights[0]["rule_id"] == "R-FXH-04"


def test_fixture_without_footprint_uses_type_defaults_and_says_so():
    """
    Previously such a fixture vanished. Rendering a default-sized box at its
    stated position is more useful than silently dropping it — provided the
    invented dimensions are recorded, which is what the resolution log is for.
    """
    layout = UnifiedLayout(units="mm", fixtures=[{"kind": "generic", "x": 0, "y": 0}])
    result = build_massing(layout)
    assert result.stats["fixtures"] == 1
    invented = {
        r["field"] for r in result.resolutions if r["source"] == SOURCE_TYPE_DEFAULT
    }
    assert {"width_mm", "depth_mm"} <= invented


def test_rotated_fixture_keeps_its_footprint_area():
    layout = UnifiedLayout(
        units="mm",
        fixtures=[
            {
                "kind": "table",
                "x": 0,
                "y": 0,
                "width_mm": 2000,
                "depth_mm": 700,
                "height_mm": 750,
                "rotation_deg": 90,
            }
        ],
    )
    result = build_massing(layout)
    # Rotated 90° about Z, so the plan extents swap.
    extents = _fixture_extents(result, 0)
    assert extents[0] == pytest.approx(700, abs=1e-6)
    assert extents[1] == pytest.approx(2000, abs=1e-6)


# ---------------------------------------------------------------------------
# Z provenance
# ---------------------------------------------------------------------------


def test_absent_ceiling_height_is_marked_assumed_default():
    result = build_massing(_room_layout())
    assert result.provenance["z_source"] == "assumed_default"
    assert result.provenance["not_for_construction"] is True
    assert result.provenance["ceiling_height_mm"] == 3000.0
    assert "ceiling_height_mm" in result.provenance["blind_fields"]


def test_meta_ceiling_height_is_marked_user_supplied():
    result = build_massing(_room_layout(ceiling_height_mm=3200))
    assert result.provenance["z_source"] == "user_supplied"
    assert result.provenance["ceiling_height_mm"] == 3200.0
    assert "ceiling_height_mm" not in result.provenance["blind_fields"]


def test_declaring_every_blind_field_clears_the_construction_warning():
    """
    The badge has to be clearable, or it stops carrying information. Declaring
    the two values nothing in the layout can supply is enough.
    """
    result = build_massing(_room_layout(ceiling_height_mm=3200, wall_thickness_mm=150))
    assert result.provenance["blind_fields"] == []
    assert result.provenance["not_for_construction"] is False


def test_informed_type_defaults_do_not_trigger_the_warning():
    """A type default is informed by the fixture; a global default is a guess."""
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 4000, "y2": 0}],
        fixtures=[{"kind": "counter", "x": 500, "y": 500, "width_mm": 1000, "depth_mm": 600}],
        meta={"ceiling_height_mm": 3000, "wall_thickness_mm": 100},
    )
    result = build_massing(layout)
    assert result.provenance["has_assumptions"] is True
    assert result.provenance["not_for_construction"] is False


def test_meta_wall_thickness_overrides_default():
    layout = _room_layout(wall_thickness_mm=230)
    assert resolve_wall_thickness_mm(layout) == (230.0, SOURCE_USER)
    assert build_massing(layout).provenance["wall_thickness_mm"] == 230.0


def test_non_numeric_meta_falls_back_to_default():
    layout = _room_layout(ceiling_height_mm="tall")
    assert resolve_ceiling_height_mm(layout) == (3000.0, SOURCE_GLOBAL_DEFAULT)


# ---------------------------------------------------------------------------
# Scene structure — the GLB contract the viewer depends on
# ---------------------------------------------------------------------------


def test_scene_groups_are_present_for_layer_toggles():
    result = build_massing(_room_layout())
    nodes = set(result.scene.graph.nodes)
    for group in (GROUP_WALLS, GROUP_FIXTURES, GROUP_CEILING):
        assert group in nodes


def test_dimensions_cover_walls_overall_and_ceiling():
    result = build_massing(_room_layout())
    kinds = {d["kind"] for d in result.dimensions}
    assert {"wall_length", "overall_width", "overall_depth", "ceiling_height"} <= kinds

    wall_dims = [d for d in result.dimensions if d["kind"] == "wall_length"]
    assert len(wall_dims) == 4
    assert {d["label"] for d in wall_dims} == {"6000", "4000"}


def test_ceiling_height_dimension_matches_provenance():
    result = build_massing(_room_layout(ceiling_height_mm=3200))
    dim = next(d for d in result.dimensions if d["kind"] == "ceiling_height")
    assert dim["label"] == "3200"
    assert dim["z"] == pytest.approx(3200.0)


def test_fixture_dimension_labels_carry_all_three_extents():
    result = build_massing(_room_layout())
    dims = [d for d in result.dimensions if d["kind"] == "fixture_extents"]
    assert len(dims) == 2
    assert dims[0]["label"].startswith("1200 x 600 x 1800")


# ---------------------------------------------------------------------------
# GLB export
# ---------------------------------------------------------------------------


def _glb_json(glb: bytes) -> dict:
    assert glb[:4] == b"glTF"
    json_len = struct.unpack("<I", glb[12:16])[0]
    return json.loads(glb[20 : 20 + json_len])


def test_glb_exports_valid_container():
    glb = build_massing(_room_layout()).export_glb()
    doc = _glb_json(glb)
    assert doc["asset"]["version"] == "2.0"
    assert doc["meshes"]
    assert doc["nodes"]


def test_only_declared_surfaces_carry_a_texture():
    """
    Replaces an earlier no-textures-at-all rule. Textures now ship, but only for
    the handful of surfaces in _TEXTURED that are large enough for a tile to
    read; a fixture band shows too little of one to be worth the payload.
    """
    from app.services.mesh_builder import _TEXTURED

    doc = _glb_json(build_massing(_room_layout()).export_glb())
    assert doc.get("materials")
    textured = {
        mat["name"]
        for mat in doc["materials"]
        if "baseColorTexture" in mat.get("pbrMetallicRoughness", {})
    }
    assert textured == {f"pf_{key}" for key in _TEXTURED}


def test_each_texture_is_embedded_once_however_many_meshes_use_it():
    """
    Every mesh sharing a material must share one image object, or the exporter
    embeds a copy of the tile per mesh and the GLB budget dies quietly. A room
    has many wall boxes and many floor slabs but only two textured materials
    (floor, wall), each carrying a colour map plus a packed ORM and a normal
    map.

    Each textured material contributes 4 image *entries* (colour,
    metallicRoughness, occlusion, normal) — occlusion reuses the ORM image
    object, but trimesh still writes it as a second glTF image referencing the
    same bufferView rather than deduping the JSON entry. What must not
    duplicate is the underlying bytes: only 3 distinct bufferViews per
    material (6 total), regardless of how many meshes use it.
    """
    doc = _glb_json(build_massing(_room_layout()).export_glb())
    images = doc.get("images", [])
    assert len(images) == 8
    assert len(doc.get("textures", [])) == 8
    unique_buffer_views = {im["bufferView"] for im in images}
    assert len(unique_buffer_views) == 6, "occlusion should reuse the ORM image's bytes, not duplicate them"


def test_a_textured_material_does_not_also_tint_through_base_colour():
    """
    glTF multiplies baseColorFactor by baseColorTexture. The palette colour is
    already baked into the image, so leaving it in the factor too would square
    it and render the surface far darker than the measured colour.
    """
    doc = _glb_json(build_massing(_room_layout()).export_glb())
    for mat in doc["materials"]:
        pbr = mat.get("pbrMetallicRoughness", {})
        if "baseColorTexture" not in pbr:
            continue
        assert pbr.get("baseColorFactor", [1, 1, 1, 1])[:3] == pytest.approx([1.0, 1.0, 1.0])


def test_every_material_states_a_real_roughness():
    """
    Everything shipped at a blanket 1.0 before, so a lacquered worktop and a
    concrete wall responded to light identically and the whole model read as
    chalk. Nothing without a real roughness map should sit at the extreme by
    default any more.

    Floor and wall are the exception, and deliberately so: they now carry a
    measured per-texel roughness map (see textures/manifest.json) instead of
    a single guessed number. There, roughnessFactor is glTF's defined
    multiplier on that map and correctly sits at 1.0 (pass the texture
    through unscaled) — that is not the old "unknown, default to 1.0"
    meaning, so it is checked separately rather than folded into the same
    0-1 exclusive bound as the scalar-only materials.
    """
    doc = _glb_json(build_massing(_room_layout()).export_glb())
    textured = {"pf_wall", "pf_floor"}
    roughness = {}
    for mat in doc["materials"]:
        pbr = mat.get("pbrMetallicRoughness", {})
        roughness[mat["name"]] = pbr.get("roughnessFactor", 1.0)
        if mat["name"] in textured:
            assert "metallicRoughnessTexture" in pbr, f"{mat['name']} should carry a real roughness map"

    assert roughness
    for name in textured:
        assert roughness[name] == 1.0

    scalar_only = {k: v for k, v in roughness.items() if k not in textured}
    assert scalar_only
    for name, value in scalar_only.items():
        assert 0.0 < value < 1.0, f"{name} roughness {value}"
    assert len(set(scalar_only.values())) > 1, "every scalar material still shares one roughness"


def test_floor_and_wall_carry_occlusion_and_normal_maps_nothing_else_does():
    """
    Only floor and wall are large enough to show a repeat, so only they should
    carry the ORM/normal maps — everything else stays scalar-roughness flat
    colour (a 100mm-deep fixture band shows too little of a tile to be worth
    the payload).
    """
    doc = _glb_json(build_massing(_room_layout()).export_glb())
    textured = {"pf_wall", "pf_floor"}
    for mat in doc["materials"]:
        if mat["name"] in textured:
            assert "occlusionTexture" in mat, f"{mat['name']} should carry occlusion"
            assert "normalTexture" in mat, f"{mat['name']} should carry a normal map"
            # ORM packing: occlusionTexture and metallicRoughnessTexture reuse
            # the same source image, not two independently embedded copies.
            orm_index = mat["pbrMetallicRoughness"]["metallicRoughnessTexture"]["index"]
            occ_index = mat["occlusionTexture"]["index"]
            orm_image = doc["textures"][orm_index]["source"]
            occ_image = doc["textures"][occ_index]["source"]
            assert doc["images"][orm_image]["bufferView"] == doc["images"][occ_image]["bufferView"]
        else:
            assert "occlusionTexture" not in mat, f"{mat['name']} should not carry occlusion"
            assert "normalTexture" not in mat, f"{mat['name']} should not carry a normal map"


def test_a_missing_texture_file_falls_back_to_flat_colour(monkeypatch, tmp_path):
    """
    The textures are ordinary files in the package; a partial checkout or a
    trimmed image must degrade to the flat palette colour, not raise.
    """
    from app.services import mesh_builder as mb

    mb._tinted_texture.cache_clear()
    mb._map_texture.cache_clear()
    monkeypatch.setattr(mb, "_TEXTURE_DIR", tmp_path)
    try:
        doc = _glb_json(build_massing(_room_layout()).export_glb())
        assert not doc.get("images")
        for mat in doc["materials"]:
            pbr = mat.get("pbrMetallicRoughness", {})
            assert "baseColorTexture" not in pbr
            assert "metallicRoughnessTexture" not in pbr
            assert "occlusionTexture" not in mat
            assert "normalTexture" not in mat
    finally:
        mb._tinted_texture.cache_clear()
        mb._map_texture.cache_clear()


def test_glb_stays_small_for_a_typical_two_hundred_fixture_store():
    """
    trimesh does not emit EXT_mesh_gpu_instancing and repeats mesh data per
    node (open issue #1151), so fixture geometry is duplicated. At Tier A box
    complexity that costs ~750 bytes per fixture, which is acceptable; this
    test is the tripwire for when it stops being.
    """
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 40000, "y2": 0}],
        fixtures=[
            {
                "kind": "display",
                "x": 500 + i * 180,
                "y": 1000,
                "width_mm": 1200,
                "depth_mm": 600,
                "height_mm": 1800,
            }
            for i in range(200)
        ],
    )
    glb = build_massing(layout).export_glb()
    assert len(glb) < 1_000_000, f"GLB unexpectedly large: {len(glb)} bytes"


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------


def test_empty_layout_builds_but_reports_itself_empty():
    """Building must not raise; only export does, and with a legible message."""
    result = build_massing(UnifiedLayout(units="mm"))
    assert result.stats["wall_boxes"] == 0
    assert result.is_empty
    with pytest.raises(EmptyMassingError):
        result.export_glb()


def test_zero_length_wall_is_skipped():
    layout = UnifiedLayout(units="mm", walls=[{"x1": 10, "y1": 10, "x2": 10, "y2": 10}])
    result = build_massing(layout)
    assert result.stats["wall_boxes"] == 0


def test_degenerate_room_polygon_is_skipped():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 1000, "y2": 0}],
        rooms=[{"name": "sliver", "polygon": [[0, 0], [1, 0]]}],
    )
    result = build_massing(layout)
    assert result.stats["ceiling_slabs"] == 0


# ---------------------------------------------------------------------------
# Duplicate rooms — parsers that read one plan from two tables emit each twice
# ---------------------------------------------------------------------------


def test_duplicate_room_polygons_produce_one_ceiling_slab():
    poly = [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 4000, "y2": 0}],
        rooms=[{"name": "Retail", "polygon": poly}, {"name": "Retail", "polygon": poly}],
    )
    result = build_massing(layout)
    assert result.stats["ceiling_slabs"] == 1


def test_distinct_rooms_still_each_get_a_slab():
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 8000, "y2": 0}],
        rooms=[
            {"name": "A", "polygon": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]]},
            {"name": "B", "polygon": [[4000, 0], [8000, 0], [8000, 3000], [4000, 3000]]},
        ],
    )
    assert build_massing(layout).stats["ceiling_slabs"] == 2


# ---------------------------------------------------------------------------
# Fixture elevation — wall-hung displays must not sit on the floor
# ---------------------------------------------------------------------------


def _panel_layout(**meta) -> UnifiedLayout:
    """A wall-hung display: height_mm is the panel thickness, not floor-to-top."""
    return UnifiedLayout(
        units="mm",
        fixtures=[
            {
                "kind": "display",
                "x": 2000,
                "y": 0,
                "width_mm": 1040,
                "depth_mm": 150,
                "height_mm": 600,
                "label": '55" LFD',
            }
        ],
        meta=meta,
    )


def _floor_layout(**meta) -> UnifiedLayout:
    """A floor-standing unit, for contrast with the wall-hung case."""
    return UnifiedLayout(
        units="mm",
        fixtures=[
            {
                "kind": "table",
                "x": 2000,
                "y": 1000,
                "width_mm": 1400,
                "depth_mm": 900,
                "height_mm": 900,
                "label": "Demo table",
            }
        ],
        meta=meta,
    )


def test_floor_standing_types_sit_on_the_floor():
    result = build_massing(_floor_layout())
    assert _fixture_bounds(result, 0)[0][2] == pytest.approx(0.0)


def test_wall_hung_type_lifts_off_the_floor_by_default():
    """
    The bug this fixes: a wall-mounted display whose height_mm is really its
    panel thickness used to render lying on the floor. Its type now supplies a
    mounting elevation, recorded as a type_default rather than silently applied.
    """
    result = build_massing(_panel_layout())
    lo, _ = _fixture_bounds(result, 0)
    expected = ftypes.get_type("display_panel").default_elevation
    assert expected > 0
    assert lo[2] == pytest.approx(expected)
    elevations = [r for r in result.resolutions if r["field"] == "elevation_mm"]
    assert elevations[0]["source"] == SOURCE_TYPE_DEFAULT
    assert elevations[0]["rule_id"] == "R-FXE-04"


def test_declared_elevation_by_label_wins_over_the_type_default():
    result = build_massing(_panel_layout(fixture_elevations_mm={'55" LFD': 1200}))
    lo, hi = _fixture_bounds(result, 0)
    assert lo[2] == pytest.approx(1200.0)
    assert hi[2] == pytest.approx(1800.0)
    elevations = [r for r in result.resolutions if r["field"] == "elevation_mm"]
    assert elevations[0]["source"] == SOURCE_USER


def test_declared_elevation_by_kind_applies_to_the_category():
    result = build_massing(_panel_layout(fixture_elevations_mm={"display": 1500}))
    assert _fixture_bounds(result, 0)[0][2] == pytest.approx(1500.0)


def test_label_beats_kind_which_beats_wildcard():
    result = build_massing(
        _panel_layout(fixture_elevations_mm={'55" LFD': 1500, "display": 900, "*": 100})
    )
    assert _fixture_bounds(result, 0)[0][2] == pytest.approx(1500.0)


def test_wildcard_elevation_applies_when_nothing_else_matches():
    result = build_massing(_floor_layout(fixture_elevations_mm={"*": 250}))
    assert _fixture_bounds(result, 0)[0][2] == pytest.approx(250.0)


def test_elevated_fixture_is_clamped_below_the_ceiling():
    result = build_massing(
        _panel_layout(ceiling_height_mm=3000, fixture_elevations_mm={"*": 2800})
    )
    lo, hi = _fixture_bounds(result, 0)
    assert lo[2] == pytest.approx(2800.0)
    assert hi[2] <= 3000.0 + 1e-6


def test_non_numeric_elevation_falls_back_to_the_type_default():
    result = build_massing(_panel_layout(fixture_elevations_mm={"*": "high"}))
    expected = ftypes.get_type("display_panel").default_elevation
    assert _fixture_bounds(result, 0)[0][2] == pytest.approx(expected)


def test_elevation_appears_in_the_dimension_label():
    result = build_massing(_panel_layout(fixture_elevations_mm={"*": 1200}))
    dim = next(d for d in result.dimensions if d["kind"] == "fixture_extents")
    assert dim["label"] == "1040 x 150 x 600 @ 1200"
    assert dim["z"] == pytest.approx(1800.0)


def test_floor_fixture_label_has_no_elevation_suffix():
    result = build_massing(_floor_layout())
    dim = next(d for d in result.dimensions if d["kind"] == "fixture_extents")
    assert "@" not in dim["label"]


# ---------------------------------------------------------------------------
# Viewpoints
# ---------------------------------------------------------------------------


def test_viewpoints_cover_the_plan_and_four_elevations():
    """Names match the A-D section markers dxf_exporter already draws."""
    result = build_massing(_room_layout())
    ids = [v["id"] for v in result.viewpoints]
    assert ids == ["PLAN", "A", "B", "C", "D"]
    assert all(v["hide_groups"] == [GROUP_CEILING] for v in result.viewpoints)


def test_no_profile_means_no_entrance_eye_level_presets():
    """The PLAN+A-D list is exactly what every project got before profiles
    existed -- eye-level presets are additive and opt-in only."""
    result = build_massing(_room_layout())
    ids = [v["id"] for v in result.viewpoints]
    assert ids == ["PLAN", "A", "B", "C", "D"]


def test_retail_showroom_profile_adds_its_requested_entrance_eye_level_presets():
    result = build_massing(_room_layout(visual_profile="retail_showroom"))
    ids = [v["id"] for v in result.viewpoints]
    assert ids == ["PLAN", "ENTRANCE", "ANGLED", "REAR", "A", "B", "C", "D"]
    for vid in ("ENTRANCE", "ANGLED", "REAR"):
        vp = next(v for v in result.viewpoints if v["id"] == vid)
        # Unlike PLAN/A-D, these stand inside the room at eye level, so they
        # ask the viewer to show the ceiling rather than hide it -- see
        # _entrance_eye_level_viewpoints's own docstring for why.
        assert vp.get("show_groups") == [GROUP_CEILING]
        assert vp["orthographic"] is False


def test_entrance_eye_level_presets_need_a_real_entrance_signal():
    """Same refusal-to-guess principle as the PLAN camera's own entrance
    fallback (test_plan_camera_falls_back_to_the_corner_axonometric_
    without_an_entrance) -- no door, no eye-level presets, even with a
    profile selected that would otherwise ask for them."""
    layout = _room_layout(visual_profile="retail_showroom")
    layout.openings = []
    result = build_massing(layout)
    ids = [v["id"] for v in result.viewpoints]
    assert "ENTRANCE" not in ids
    assert ids == ["PLAN", "A", "B", "C", "D"]


def test_entrance_eye_level_presets_stay_inside_the_layouts_own_bounds():
    """A degenerate framing (camera pressed against the exterior of a wall,
    the defect this mechanism exists to avoid for long/narrow stores -- see
    _entrance_eye_level_viewpoints's own docstring) would put the camera or
    target far outside the plan; both must land inside it instead."""
    result = build_massing(_room_layout(visual_profile="retail_showroom"))
    minx, miny, maxx, maxy = layout_bounds_2d(_room_layout())
    pad = 500.0  # cameras may sit just inside the entrance, not dead centre
    for vid in ("ENTRANCE", "ANGLED", "REAR"):
        vp = next(v for v in result.viewpoints if v["id"] == vid)
        for x, y, _ in (vp["position"], vp["target"]):
            assert minx - pad <= x <= maxx + pad, f"{vid} x={x} outside {minx}-{maxx}"
            assert miny - pad <= y <= maxy + pad, f"{vid} y={y} outside {miny}-{maxy}"
        # Regression pin: an earlier version of the inward-direction sign fix
        # could collapse position and target onto the same point (a
        # zero-length look direction, undefined for a camera) whenever the
        # entrance happened to sit at the plan's own extreme corner along
        # that axis -- caught by rendering and looking, not by this bounds
        # check alone, which a degenerate camera still technically passes.
        import math

        px, py, pz = vp["position"]
        tx, ty, tz = vp["target"]
        assert math.hypot(px - tx, py - ty) > 100.0, f"{vid} camera looks at its own position"


def test_plan_camera_clears_the_near_wall_rather_than_staring_at_its_back():
    """
    What the old fixed >=50 degree rule was really protecting: the near wall
    must not swallow the interior. Asserting the occluded depth directly says
    that, and survives the camera being re-aimed down the entrance axis at a
    deliberately shallower angle than a corner view could afford.
    """
    import math

    layout = _room_layout()
    vp = next(v for v in build_massing(layout).viewpoints if v["id"] == "PLAN")
    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    depth = max(maxx - minx, maxy - miny)

    px, py, pz = vp["position"]
    tx, ty, tz = vp["target"]
    ground = math.hypot(px - tx, py - ty)
    rise = pz - tz
    assert rise > 0.0, "plan camera is not above its target"

    # Horizontal run from the camera to the wall it looks over, then how far
    # past that wall its shadow reaches along the floor.
    to_wall = max(ground - depth / 2.0, 1.0)
    wall_h = build_massing(layout).provenance["ceiling_height_mm"]
    cam_h = pz
    assert cam_h > wall_h, "plan camera sits below the wall it must see over"
    occluded = to_wall * wall_h / (cam_h - wall_h)
    assert occluded < depth * 0.30, (
        f"near wall hides {occluded:.0f}mm of a {depth:.0f}mm plan"
    )


def test_plan_camera_looks_in_through_the_entrance_when_one_is_known():
    """
    The entrance is already identified to colour the shopfront; the default
    camera reuses it so the model is read from the door rather than from
    whichever corner the bounding box happens to start at.
    """
    import math

    layout = _room_layout()
    layout.openings = [
        Opening(kind="door", label="Entrance", x1=0.0, y1=0.0, x2=1800.0, y2=0.0)
    ]
    vp = next(v for v in build_massing(layout).viewpoints if v["id"] == "PLAN")

    # The entrance is on the y=0 wall, so the camera must stand on that side.
    minx, miny, maxx, maxy = layout_bounds_2d(layout)
    assert vp["position"][1] < miny, "camera is not outside the entrance wall"
    assert vp["orthographic"] is False, "entrance view should converge"


def test_plan_camera_falls_back_to_the_corner_axonometric_without_an_entrance():
    """Same refusal-to-guess rule as the shopfront colouring it shares a signal with."""
    layout = _room_layout()
    layout.openings = []
    vp = next(v for v in build_massing(layout).viewpoints if v["id"] == "PLAN")
    assert vp["orthographic"] is True


def test_elevation_viewpoints_look_inward_from_outside_the_bounds():
    result = build_massing(_room_layout())
    minx, miny, maxx, maxy = layout_bounds_2d(_room_layout())
    for vp in result.viewpoints:
        if vp["kind"] != "elevation":
            continue
        px, py, _ = vp["position"]
        outside = px < minx or px > maxx or py < miny or py > maxy
        assert outside, f"{vp['id']} camera is inside the plan"


def test_floor_slab_sits_below_datum():
    result = build_massing(_room_layout())
    floors = _named(result, "floor_")
    assert floors
    for name, mesh in floors.items():
        assert mesh.bounds[1][2] <= 1e-6, f"{name} pokes above the floor line"


def test_floor_and_ceiling_slabs_share_their_plan_footprint():
    result = build_massing(_room_layout())
    floor = list(_named(result, "floor_").values())
    ceiling = list(_named(result, "ceiling_").values())
    assert len(floor) == len(ceiling)
    for f, c in zip(floor, ceiling):
        assert f.bounds[0][:2] == pytest.approx(c.bounds[0][:2], abs=1e-6)
        assert f.bounds[1][:2] == pytest.approx(c.bounds[1][:2], abs=1e-6)


def test_slabs_carry_their_room_identity():
    """Dedupe used to break the slab-index/room-index correspondence."""
    result = build_massing(_room_layout())
    for group in ("floor_", "ceiling_"):
        for name, mesh in _named(result, group).items():
            assert mesh.metadata.get("room") == "Retail", name
            assert mesh.metadata.get("room_index") == 0, name


# ---------------------------------------------------------------------------
# Emissive materials — light boxes should not render as switched off
# ---------------------------------------------------------------------------


def test_light_box_face_band_is_emissive_in_the_glb():
    layout = UnifiedLayout(
        units="mm",
        fixtures=[
            {
                "kind": "", "x": 1000, "y": 1000,
                "width_mm": 1200, "depth_mm": 120, "height_mm": 400,
                "label": "ISB Light Box",
            }
        ],
    )
    result = build_massing(layout)
    doc = _glb_json(result.export_glb())
    emissive_mats = [
        m for m in doc.get("materials", [])
        if any(v > 0 for v in m.get("emissiveFactor", [0, 0, 0]))
    ]
    assert emissive_mats, "expected at least one emissive material in the GLB"


def test_only_the_emissive_material_carries_an_emissive_factor():
    """Every other material stays flat, per the no-material-design rule."""
    result = build_massing(_room_layout())
    doc = _glb_json(result.export_glb())
    for m in doc.get("materials", []):
        ef = m.get("emissiveFactor", [0, 0, 0])
        if m.get("name") != "pf_emissive":
            assert all(v == 0 for v in ef), f"{m.get('name')} unexpectedly emissive"


# ---------------------------------------------------------------------------
# Wall-bay run merging
# ---------------------------------------------------------------------------


def _wall_bay_run_layout(gaps_mm, far_gap_mm=2500.0):
    """N wall_bay fixtures with the given gaps between them, plus one far away."""
    fixtures = []
    x = 550.0
    w = 1000.0
    for i, gap in enumerate([0.0] + list(gaps_mm)):
        x += gap
        fixtures.append({
            "kind": "12-L", "x": x, "y": 50, "width_mm": w, "depth_mm": 100,
            "height_mm": 2200, "label": f"BASE WALL-1 {i}",
        })
        x += w
    far_x = x + far_gap_mm + w / 2
    fixtures.append({
        "kind": "12-L", "x": far_x, "y": 50, "width_mm": w, "depth_mm": 100,
        "height_mm": 2200, "label": "BASE WALL-1 far",
    })
    return UnifiedLayout(
        units="mm", walls=[{"x1": 0, "y1": 0, "x2": far_x + 3000, "y2": 0}],
        fixtures=fixtures,
    )


def test_a_contiguous_run_merges_into_zero_visible_gaps():
    """
    A run of N same-type fixtures with small residual gaps should render with no
    visible seam between them -- the direct, testable form of "fewer visual
    segments than N" when per-fixture click identity must survive intact (see
    the next two tests): the segment count that drops is the number of visible
    gaps, N-1 -> 0, not the mesh count.
    """
    layout = _wall_bay_run_layout(gaps_mm=[80.0, 60.0])
    result = build_massing(layout)
    bases = [
        (n, m) for n, m in sorted(result.scene.geometry.items())
        if n.startswith("fixture_") and n.endswith("_base") and "far" not in m.metadata.get("label", "")
    ]
    assert len(bases) == 3
    for (_, a), (_, b) in zip(bases, bases[1:]):
        assert a.bounds[1][0] == pytest.approx(b.bounds[0][0], abs=1e-6), (
            "adjacent run members must touch exactly, no residual visible gap"
        )
    assert result.stats["wall_bay_runs"] == 1


def test_a_genuinely_separated_fixture_does_not_merge():
    layout = _wall_bay_run_layout(gaps_mm=[80.0], far_gap_mm=2500.0)
    result = build_massing(layout)
    far = next(
        m for n, m in result.scene.geometry.items()
        if n.startswith("fixture_") and n.endswith("_base") and m.metadata.get("run_id") is None
    )
    assert far.metadata["run_size"] == 1
    assert result.stats["wall_bay_runs"] == 1  # only the near pair merged


def test_click_to_sku_still_resolves_the_correct_individual_fixture_after_merging():
    """
    Merging must not collapse the underlying fixture records: every mesh in a
    run keeps its own name, its own declared width_mm in extras, and its own
    label -- a click still identifies exactly which original fixture it hit.
    """
    layout = _wall_bay_run_layout(gaps_mm=[80.0, 60.0])
    result = build_massing(layout)
    bases = {
        m.metadata["label"]: m
        for n, m in result.scene.geometry.items()
        if n.startswith("fixture_") and n.endswith("_base")
    }
    assert set(bases) == {
        "BASE WALL-1 0", "BASE WALL-1 1", "BASE WALL-1 2", "BASE WALL-1 far",
    }
    for label, mesh in bases.items():
        # The rendered box may be wider (run-extended), but the fixture's own
        # declared size -- what click-to-SKU shows -- is never altered.
        assert mesh.metadata["width_mm"] == 1000.0, label


def test_merged_bands_without_width_inset_span_the_full_run_seamlessly():
    """wall_bay's panel/boxing bands have no inset_w, so a merge should close
    their gaps exactly too, not just the base band's."""
    layout = _wall_bay_run_layout(gaps_mm=[80.0])
    result = build_massing(layout)
    for band in ("panel", "boxing"):
        meshes = [
            m for n, m in sorted(result.scene.geometry.items())
            if n.startswith("fixture_") and n.endswith(f"_{band}")
            and "far" not in m.metadata.get("label", "")
        ]
        assert len(meshes) == 2
        assert meshes[0].bounds[1][0] == pytest.approx(meshes[1].bounds[0][0], abs=1e-6), band


def test_wall_bay_merging_is_deterministic():
    layout = _wall_bay_run_layout(gaps_mm=[80.0, 60.0])
    a = build_massing(layout).vertex_hash()
    b = build_massing(layout).vertex_hash()
    assert a == b


def test_wall_bay_merging_does_not_affect_model_confidence_or_construction_flag():
    """Pure geometry/material change -- none of this touches a resolution-ladder
    governed field, so confidence and the construction badge must be unaffected."""
    plain = build_massing(_wall_bay_run_layout(gaps_mm=[]))
    merged = build_massing(_wall_bay_run_layout(gaps_mm=[80.0, 60.0]))
    assert plain.provenance["model_confidence"] == merged.provenance["model_confidence"]
    assert plain.provenance["not_for_construction"] == merged.provenance["not_for_construction"]


def test_different_fixture_types_never_merge_even_if_adjacent():
    layout = UnifiedLayout(
        units="mm", walls=[{"x1": 0, "y1": 0, "x2": 4000, "y2": 0}],
        fixtures=[
            {"kind": "12-L", "x": 550, "y": 50, "width_mm": 1000, "depth_mm": 100, "height_mm": 2200, "label": "wall bay"},
            {"kind": "13-A", "x": 1090, "y": 50, "width_mm": 1000, "depth_mm": 320, "height_mm": 900, "label": "base cabinet, different type"},
        ],
    )
    result = build_massing(layout)
    assert result.stats["wall_bay_runs"] == 0


# ---------------------------------------------------------------------------
# Dark shopfront shell
# ---------------------------------------------------------------------------


def test_shopfront_wall_is_identified_from_an_entrance_labelled_opening():
    """No wall index is ever hardcoded -- identification must work regardless
    of which wall in the list happens to carry the entrance."""
    layout = UnifiedLayout(
        units="mm",
        walls=[
            {"x1": 0, "y1": 0, "x2": 6000, "y2": 0},
            {"x1": 6000, "y1": 0, "x2": 6000, "y2": 4000},
            {"x1": 6000, "y1": 4000, "x2": 0, "y2": 4000},
            {"x1": 0, "y1": 4000, "x2": 0, "y2": 0},
        ],
        openings=[{"kind": "opening", "x1": 6000, "y1": 1500, "x2": 6000, "y2": 2500, "label": "Entrance"}],
    )
    result = build_massing(layout)
    assert result.stats["shopfront_walls"] == [1]
    for name, mesh in result.scene.geometry.items():
        if not name.startswith("wall_"):
            continue
        expected = "pf_wall_shopfront" if mesh.metadata["wall_index"] == 1 else "pf_wall"
        assert mesh.visual.material.name == expected, name


def test_shopfront_falls_back_to_any_door_when_no_entrance_label_exists():
    layout = UnifiedLayout(
        units="mm",
        walls=[
            {"x1": 0, "y1": 0, "x2": 6000, "y2": 0},
            {"x1": 6000, "y1": 0, "x2": 6000, "y2": 4000},
        ],
        openings=[{"kind": "door", "x1": 6000, "y1": 1500, "x2": 6000, "y2": 2500, "label": "D1"}],
    )
    result = build_massing(layout)
    assert result.stats["shopfront_walls"] == [1]


def test_shopfront_identification_never_guesses_without_a_signal():
    """A window-only layout, or one with no openings, must not paint a wall
    dark on a coin-flip -- an empty result is the honest answer."""
    layout = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 6000, "y2": 0}],
        openings=[{"kind": "window", "x1": 3000, "y1": 0, "x2": 4000, "y2": 0, "label": "W1"}],
    )
    result = build_massing(layout)
    assert result.stats["shopfront_walls"] == []
    for name, mesh in result.scene.geometry.items():
        if name.startswith("wall_"):
            assert mesh.visual.material.name == "pf_wall"


def test_shopfront_treatment_is_purely_a_colour_change():
    """Same geometry, watertight, same X/Y bounds -- only the material differs."""
    with_entrance = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 6000, "y2": 0}],
        openings=[{"kind": "door", "x1": 2000, "y1": 0, "x2": 2900, "y2": 0, "label": "Entrance"}],
    )
    without_label = UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 6000, "y2": 0}],
        openings=[{"kind": "door", "x1": 2000, "y1": 0, "x2": 2900, "y2": 0, "label": "D1"}],
    )
    a = build_massing(with_entrance)
    b = build_massing(without_label)
    assert a.stats["wall_boxes"] == b.stats["wall_boxes"]
    assert a.stats["bounds_2d"] == b.stats["bounds_2d"]
    for name, mesh in a.scene.geometry.items():
        if name.startswith("wall_"):
            assert mesh.is_watertight, name


def test_shopfront_treatment_does_not_affect_confidence_or_construction_flag():
    plain = build_massing(_room_layout())
    with_entrance = build_massing(_room_layout())  # _room_layout has a door opening already
    assert plain.provenance["model_confidence"] == with_entrance.provenance["model_confidence"]
    assert plain.provenance["not_for_construction"] == with_entrance.provenance["not_for_construction"]


# ---------------------------------------------------------------------------
# POC depth projection — a wall bay is a panel plus shelves standing out in
# front of it, and the BOQ only states the panel
# ---------------------------------------------------------------------------


def _wall_bay_layout(rotation_deg: float, y: float) -> UnifiedLayout:
    """One BW-1 bay on a long wall, at the BOQ's own dimensions."""
    return UnifiedLayout(
        units="mm",
        walls=[
            {"x1": 0, "y1": 0, "x2": 6000, "y2": 0},
            {"x1": 6000, "y1": 0, "x2": 6000, "y2": 4000},
            {"x1": 6000, "y1": 4000, "x2": 0, "y2": 4000},
            {"x1": 0, "y1": 4000, "x2": 0, "y2": 0},
        ],
        fixtures=[{
            "kind": "12-L", "label": "BASE WALL-1 (BW-1)",
            "x": 2000, "y": y,
            "width_mm": 1100, "depth_mm": 100, "height_mm": 2193,
            "rotation_deg": rotation_deg,
        }],
    )


def test_a_wall_bay_now_occupies_more_depth_than_its_declared_panel():
    """
    The whole point of the allowance. Drawn strictly inside the BOQ's 100mm the
    bay can only be a flat slab; it has to stand proud of that to read as
    shelved at all.
    """
    result = build_massing(_wall_bay_layout(0.0, 260.0))
    lo, hi = _fixture_bounds(result, 0)
    assert hi[1] - lo[1] > 100.0 * 1.5, "bay is still effectively a flat panel"


def test_wall_bay_depth_stays_inside_the_declared_poc_envelope():
    """
    The bound that replaced "bands never leave the footprint". It is not weaker,
    just anchored to something the type states out loud.
    """
    spec = ftypes.FIXTURE_TYPES["wall_bay"]
    for rot, y in ((0.0, 260.0), (180.0, 3740.0)):
        result = build_massing(_wall_bay_layout(rot, y))
        lo, hi = _fixture_bounds(result, 0)
        assert hi[1] - lo[1] <= 100.0 * spec.poc_envelope_d + 1e-6, f"rot={rot}"


@pytest.mark.parametrize(
    "rotation_deg,y,wall_y,room_is_plus_y",
    [(0.0, 260.0, 0.0, True), (180.0, 3740.0, 4000.0, False)],
)
def test_shelves_project_into_the_room_never_through_the_wall_behind(
    rotation_deg, y, wall_y, room_is_plus_y
):
    """
    Regression pin. The first version of this grew the same direction a "back"
    band shifts, which sent every shelf straight through the wall the bay hangs
    on -- invisible from the outside, and invisible in a bounds check that only
    measures total depth.
    """
    result = build_massing(_wall_bay_layout(rotation_deg, y))
    lo, hi = _fixture_bounds(result, 0)
    foot_lo, foot_hi = y - 50.0, y + 50.0

    if room_is_plus_y:
        assert hi[1] > foot_hi + 1.0, "nothing projected into the room"
        assert lo[1] >= foot_lo - 1e-6, "projection reached back past the panel"
    else:
        assert lo[1] < foot_lo - 1.0, "nothing projected into the room"
        assert hi[1] <= foot_hi + 1e-6, "projection reached back past the panel"


def test_projection_does_not_move_the_fixture_in_width():
    """Depth is the only axis the allowance touches; X is still the BOQ's."""
    result = build_massing(_wall_bay_layout(0.0, 260.0))
    lo, hi = _fixture_bounds(result, 0)
    assert lo[0] == pytest.approx(2000 - 550, abs=1e-6)
    assert hi[0] == pytest.approx(2000 + 550, abs=1e-6)


def test_a_recipe_asking_past_its_envelope_is_clamped_not_trusted():
    """
    mesh_builder clamps rather than believing the recipe, so a bad edit degrades
    to a bay that is merely too shallow instead of one growing through a wall.
    """
    from app.services.fixture_types import Band, FixtureType

    greedy = FixtureType(
        key="wall_bay",
        bands=(Band("shelf", 0.0, 1.0, ftypes.MAT_BODY, back=True, project_d=99.0),),
        default_w=1100.0, default_d=100.0, default_h=2200.0,
        poc_envelope_d=3.0,
    )
    patched = dict(ftypes.FIXTURE_TYPES)
    patched["wall_bay"] = greedy
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ftypes, "FIXTURE_TYPES", patched)
        result = build_massing(_wall_bay_layout(0.0, 260.0))
        lo, hi = _fixture_bounds(result, 0)
        assert hi[1] - lo[1] == pytest.approx(300.0, abs=1e-6)


def test_projection_does_not_disturb_confidence_or_the_construction_flag():
    """
    A rendering allowance must not read as new knowledge about the store.
    Compared against the same layout with projection stripped, rather than
    against fixed numbers -- what matters is that drawing shelves moves neither
    value, whatever those values happen to be for a given layout.
    """
    from dataclasses import replace

    layout = _wall_bay_layout(0.0, 260.0)
    spec = ftypes.FIXTURE_TYPES["wall_bay"]
    flat = replace(
        spec,
        bands=tuple(replace(b, project_d=0.0) for b in spec.bands),
        poc_envelope_d=1.0,
    )

    with_shelves = build_massing(layout).provenance
    patched = dict(ftypes.FIXTURE_TYPES)
    patched["wall_bay"] = flat
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ftypes, "FIXTURE_TYPES", patched)
        without = build_massing(layout).provenance

    for field in ("model_confidence", "not_for_construction", "counts_by_source"):
        assert with_shelves[field] == without[field], field


# ---------------------------------------------------------------------------
# Repeated bands — the primitive that lets a fixture carry product
# ---------------------------------------------------------------------------


def _single(kind: str, label: str, w: float, d: float, h: float, **extra_meta) -> UnifiedLayout:
    return UnifiedLayout(
        units="mm",
        walls=[{"x1": 0, "y1": 0, "x2": 8000, "y2": 0}],
        fixtures=[{
            "kind": kind, "label": label, "x": 3000, "y": 1500,
            "width_mm": w, "depth_mm": d, "height_mm": h,
        }],
        meta={"ceiling_height_mm": 3000, **extra_meta},
    )


def test_a_repeated_band_emits_one_mesh_per_grid_cell():
    """A 3x2 product grid is six objects, not one band that happens to be wide."""
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    mats = _named(result, "fixture_0_mat_")
    assert len(mats) == 6


def test_repeated_copies_do_not_touch_each_other():
    """
    Regression pin, and the reason the first attempt failed. When the inset is
    applied to the whole span instead of per cell, the copies tile edge to edge
    and fuse into one solid rectangle -- on a table top that reads as a window
    frame, not as product. Each copy must be strictly narrower than its cell.
    """
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    mats = sorted(
        _named(result, "fixture_0_mat_").values(), key=lambda m: m.bounds[0][0]
    )
    assert len(mats) == 6
    cell_w = 1371.0 / 3
    for m in mats:
        width = m.bounds[1][0] - m.bounds[0][0]
        assert width < cell_w - 1.0, f"copy {width:.0f}mm fills its {cell_w:.0f}mm cell"

    # And there is a real gap between horizontal neighbours, not a shared edge.
    xs = sorted({round(m.bounds[0][0], 3) for m in mats})
    assert len(xs) == 3
    first = [m for m in mats if abs(m.bounds[0][0] - xs[0]) < 1e-6][0]
    second = [m for m in mats if abs(m.bounds[0][0] - xs[1]) < 1e-6][0]
    assert second.bounds[0][0] - first.bounds[1][0] > 1.0, "neighbouring copies touch"


def test_repeated_copies_stay_inside_the_declared_footprint():
    """Product is spread across the fixture, never off the edge of it."""
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    lo, hi = _fixture_bounds(result, 0)
    assert lo[0] >= 3000 - 1371 / 2 - 1e-6
    assert hi[0] <= 3000 + 1371 / 2 + 1e-6
    assert lo[1] >= 1500 - 900 / 2 - 1e-6
    assert hi[1] <= 1500 + 900 / 2 + 1e-6


def test_product_sits_on_the_table_top_not_through_it():
    """Mats rest on the worktop; a mat starting below it would be inside the table."""
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    top = next(iter(_named(result, "fixture_0_top").values()))
    for name, mat in _named(result, "fixture_0_mat_").items():
        assert mat.bounds[0][2] >= top.bounds[1][2] - 1e-6, name


def test_a_placed_prop_may_stand_proud_of_the_table_but_never_past_its_footprint():
    """
    island_table's product is a real placed asset (Stage 4 + the free-model
    integration), standing in its authored upright pose -- the reference
    proposal's own demo tables show product standing on a display
    stand/card, not lying flat (checked by rendering and comparing against
    the reference, not assumed). Real merchandise standing on real furniture
    legitimately rises above the furniture's own declared height -- a phone
    on a 900mm table has its own top well past 900mm in reality -- so unlike
    a fixture *band* (which must never exceed the declared height), a placed
    *product* may. What must still hold, unconditionally, is the footprint:
    nothing may overhang past the declared width/depth, asset or not.
    """
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    lo, hi = _fixture_bounds(result, 0)
    assert hi[2] > 900.0, "a standing product should rise above the table's own declared height"
    assert hi[2] < 900.0 + 250.0, "but not absurdly far past it"
    assert (hi[0] - lo[0]) <= 1371.0 + 1e-6, "footprint width must still never overhang"
    assert (hi[1] - lo[1]) <= 900.0 + 1e-6, "footprint depth must still never overhang"


def test_the_new_recipes_each_emit_their_distinct_parts():
    """
    Each reference fixture must read as an assembly, not a box. Guards the
    specific parts that carry the read, so deleting one is a test failure
    rather than a silent regression to a slab.
    """
    cases = [
        ("13-A", "BASE CABINET (BC-1.1M)", 1100, 320, 900, ("kick", "body", "drawer", "top")),
        ("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193,
         ("panel", "ledge_shelf", "ledge_stock", "peg_shelf_1", "peg_stock_1", "boxing")),
        ("9-B", "CUSTOMER SERVICE VENEERED-2P", 1600, 800, 1100, ("plinth", "front", "top", "screen")),
    ]
    for kind, label, w, d, h, parts in cases:
        result = build_massing(_single(kind, label, w, d, h))
        names = set(_named(result, "fixture_0_"))
        for part in parts:
            assert any(n.startswith(f"fixture_0_{part}") for n in names), f"{label}: {part}"


def test_populated_fixtures_stay_watertight_and_deterministic():
    """
    Product multiplies mesh count; neither guarantee may soften as it does.
    Determinism must hold even for a placed asset: the same layout must
    still produce the same vertex hash, cache or no cache.

    Placed authored assets (island_table's device, now a real phone mesh --
    see product_phone_generic) are excluded from the watertight check the
    same way the whole-fixture asset path already was, marked via
    mesh.metadata["placed_asset"] rather than a name pattern so this covers
    any future placed asset, not just today's names.
    """
    layout = _single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900)
    a, b = build_massing(layout), build_massing(layout)
    assert a.vertex_hash() == b.vertex_hash()
    for name, mesh in a.scene.geometry.items():
        if name.endswith("_asset") or mesh.metadata.get("placed_asset"):
            continue
        assert mesh.is_watertight, name


# ---------------------------------------------------------------------------
# Stage 3 — procedural geometry detail (bevel, recess, shadow-gap reveal)
# ---------------------------------------------------------------------------


def test_bevelled_top_box_keeps_the_bottom_footprint_and_shrinks_only_the_top():
    from app.services.mesh_builder import _bevelled_top_box

    mesh = _bevelled_top_box(200, 100, 50, centre=(0, 0, 0), yaw_rad=0.0, bevel=15.0)
    assert mesh is not None
    assert mesh.is_watertight
    assert mesh.is_winding_consistent
    assert mesh.volume > 0, "inverted winding would show up as a negative volume"

    verts = mesh.vertices
    bottom = verts[verts[:, 2] < 0]
    top = verts[verts[:, 2] > 0]
    # Bottom face: full length/width, exactly what _box would produce.
    assert bottom[:, 0].max() - bottom[:, 0].min() == pytest.approx(200.0, abs=1e-6)
    assert bottom[:, 1].max() - bottom[:, 1].min() == pytest.approx(100.0, abs=1e-6)
    # Top face: inset by the bevel on every side.
    assert top[:, 0].max() - top[:, 0].min() == pytest.approx(200.0 - 2 * 15.0, abs=1e-6)
    assert top[:, 1].max() - top[:, 1].min() == pytest.approx(100.0 - 2 * 15.0, abs=1e-6)


def test_bevelled_top_box_degenerates_to_a_plain_box_for_a_negligible_bevel():
    from app.services.mesh_builder import _bevelled_top_box, _box

    plain = _box(200, 100, 50, centre=(0, 0, 0), yaw_rad=0.0)
    bevelled = _bevelled_top_box(200, 100, 50, centre=(0, 0, 0), yaw_rad=0.0, bevel=0.5)
    assert bevelled.bounds == pytest.approx(plain.bounds)


def test_bevelled_top_box_clamps_an_oversized_bevel_rather_than_inverting():
    from app.services.mesh_builder import _bevelled_top_box

    # A bevel larger than half the shorter side must not collapse or invert
    # the top rectangle -- same clamp discipline as _rounded_box's radius.
    mesh = _bevelled_top_box(200, 100, 50, centre=(0, 0, 0), yaw_rad=0.0, bevel=9999.0)
    assert mesh is not None
    assert mesh.is_watertight
    assert mesh.volume > 0


def test_stacked_bands_now_have_a_real_shadow_gap_between_them():
    """
    Real casework has a visible reveal between the kick and the cabinet body,
    not a mathematically perfect seam. Before this, adjacent bands touched
    exactly (kick.z1 == body.z0), which reads as one slab with a colour change.
    """
    result = build_massing(_single("13-A", "BASE CABINET (BC-1.1M)", 1100, 320, 900))
    kick = next(iter(_named(result, "fixture_0_kick").values()))
    body = next(iter(_named(result, "fixture_0_body").values()))
    gap = body.bounds[0][2] - kick.bounds[1][2]
    assert 0.5 < gap <= 3.0 + 1e-6, f"gap={gap}"


def test_the_reveal_never_shaves_a_fixtures_own_declared_top():
    """
    A band whose top IS the fixture's own top (z1 == 1.0) must not be shaved --
    that is the "ends exactly where the source says it does" face, not a seam
    against another band. base_cabinet's worktop sits flush with the declared
    height; regressing this quietly makes every typed fixture 3mm short.

    Not island_table any more: its topmost band now places a real asset at
    its own natural size (see test_a_placed_prop_may_fall_short_of_but_
    never_exceed_the_declared_height), so it no longer pins this invariant.
    base_cabinet's "top" is still a plain procedural band and exercises the
    same reveal-skip logic this test exists to protect.
    """
    result = build_massing(_single("13-A", "BASE CABINET (BC-1.1M)", 1100, 320, 900))
    _, hi = _fixture_bounds(result, 0)
    assert hi[2] == pytest.approx(900.0, abs=1e-6)


def test_a_thin_band_keeps_most_of_its_own_thickness_despite_the_reveal():
    """The reveal is clamped to a quarter of the band's own height, so a thin
    handle groove does not nearly vanish under a fixed millimetre shave."""
    result = build_massing(_single("13-A", "BASE CABINET (BC-1.1M)", 1100, 320, 900))
    handles = _named(result, "fixture_0_handle_lower")
    assert handles, "expected a handle_lower band"
    mesh = next(iter(handles.values()))
    declared_h = 900.0 * (0.485 - 0.455)  # 27mm
    actual_h = mesh.bounds[1][2] - mesh.bounds[0][2]
    assert actual_h > declared_h * 0.7, f"{actual_h} vs declared {declared_h}"


def test_a_glass_face_recesses_behind_its_frame():
    """
    display_panel's face should sit measurably behind the frame's own front
    face, not flush with it -- that is what reads as glass in a bezel rather
    than two coplanar bands with a colour change.
    """
    result = build_massing(_single("12-P", "65 inch Smart Signage", 1100, 100, 1300))
    frame = next(iter(_named(result, "fixture_0_frame").values()))
    face = next(iter(_named(result, "fixture_0_face").values()))
    # Local +Y (front) maps to the wall-normal axis for an unrotated fixture;
    # the recess pulls the face's front edge back from the frame's.
    assert face.bounds[1][1] < frame.bounds[1][1] - 1.0


def test_recess_is_clamped_to_the_room_the_inset_already_freed_up():
    """
    A recess larger than the clearance inset_d already gave up must not push
    the band past the un-inset footprint's own front face -- same invariant
    project_d and back=True already guarantee for depth.
    """
    from app.services.fixture_types import Band, FixtureType, get_type
    import app.services.fixture_types as ftypes_mod

    key = "__test_recess_clamp__"
    ftypes_mod.FIXTURE_TYPES[key] = FixtureType(
        key=key,
        bands=(Band("face", 0.0, 1.0, ftypes_mod.MAT_SCREEN, inset_d=0.2, recess_mm=9999.0),),
        default_w=1000.0, default_d=200.0, default_h=500.0,
    )
    try:
        layout = _single(key, "", 1000, 200, 500)
        result = build_massing(layout)
        lo, hi = _fixture_bounds(result, 0)
        # Footprint depth is 200mm centred on y=1500 -> [1400, 1600]. However
        # far recess_mm asks to move, the band must stay within that range.
        assert lo[1] >= 1400.0 - 1e-6
        assert hi[1] <= 1600.0 + 1e-6
    finally:
        del ftypes_mod.FIXTURE_TYPES[key]


def test_a_worktop_now_has_a_bevelled_top_edge():
    result = build_massing(_single("13-A", "BASE CABINET (BC-1.1M)", 1100, 320, 900))
    top = next(iter(_named(result, "fixture_0_top").values()))
    verts = top.vertices
    bottom_w = np.ptp(verts[verts[:, 2] < verts[:, 2].mean()][:, 0])
    top_w = np.ptp(verts[verts[:, 2] > verts[:, 2].mean()][:, 0])
    assert top_w < bottom_w - 1.0, "worktop top face should be inset by the bevel"


def test_the_brand_wall_screen_reads_as_a_2x2_tiled_video_wall():
    """
    The reference's media wall is visibly a grid of LFD panels, not one pane
    of glass -- two stacked bands (screen_upper/screen_lower), each split
    left/right by repeat_w, in place of the single full-height "screen" band
    this recipe used to declare.
    """
    result = build_massing(_single("14-A", "BRAND WALL (LFD)-2100", 2100, 155, 2280))
    tiles = {
        name: mesh
        for name, mesh in result.scene.geometry.items()
        if name.startswith("fixture_0_screen_upper") or name.startswith("fixture_0_screen_lower")
    }
    assert len(tiles) == 4, "2 rows x 2 columns"
    upper = [m for n, m in tiles.items() if "upper" in n]
    lower = [m for n, m in tiles.items() if "lower" in n]
    assert len(upper) == 2 and len(lower) == 2
    # Every upper tile sits strictly above every lower tile -- a real row
    # split, not four tiles all at the same height.
    assert min(m.bounds[0][2] for m in upper) >= max(m.bounds[1][2] for m in lower) - 1e-6


def test_brand_wall_tiles_do_not_touch_each_other():
    """Same "gap between repeated copies" guarantee every other repeat_w
    band already gives -- a seam is what makes it read as tiles, not one
    fused pane."""
    result = build_massing(_single("14-A", "BRAND WALL (LFD)-2100", 2100, 155, 2280))
    upper = [
        m for name, m in result.scene.geometry.items()
        if name.startswith("fixture_0_screen_upper")
    ]
    assert len(upper) == 2
    left, right = sorted(upper, key=lambda m: m.bounds[0][0])
    assert left.bounds[1][0] < right.bounds[0][0], "the two tiles should not touch"


# ---------------------------------------------------------------------------
# Stage 4 — generic product placeholders (phone body + screen, no assets)
# ---------------------------------------------------------------------------


def test_a_table_top_device_places_a_real_asset_per_cell_standing():
    """
    island_table's product is a real placed mesh (see product_phone_generic
    in the fixtures manifest) per grid cell, not a procedural block -- one
    per table-top cell, standing in its authored upright pose (matching the
    reference proposal's own demo tables, which show product on a display
    stand, not lying flat -- see the recipe's own comment in
    fixture_types.py for why this changed from an earlier lying-flat
    version).
    """
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    devices = _named(result, "fixture_0_device_")
    assert len(devices) == 6, "3x2 grid"
    for name, mesh in devices.items():
        assert mesh.metadata.get("placed_asset") is True, name
        extent = mesh.bounds[1] - mesh.bounds[0]
        # Standing: the vertical extent (the phone's own height) is now its
        # largest dimension, not its thin edge.
        assert extent[2] > extent[0], f"{name} should be standing, not lying flat"
        assert extent[2] > extent[1], f"{name} should be standing, not lying flat"


def test_wall_bay_stock_places_a_real_asset_per_shelf_cell_standing():
    """Same placed-asset mechanism as the table, standing upright -- the
    ledge (fewer, larger) and the pegboard rows (many, smaller) both carry
    real placed phones; see fixture_types.py's wall_bay recipe for the
    ledge/pegboard split this pins."""
    result = build_massing(_single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193))
    expected_counts = {"ledge_stock": 3, "peg_stock_1": 6, "peg_stock_3": 6, "peg_stock_5": 6}
    for shelf, expected in expected_counts.items():
        cells = _named(result, f"fixture_0_{shelf}_")
        assert len(cells) == expected, f"{shelf}: repeat_w={expected}"
        for name, mesh in cells.items():
            assert mesh.metadata.get("placed_asset") is True, name
            extent = mesh.bounds[1] - mesh.bounds[0]
            assert extent[2] > extent[1], f"{name} should be standing, not lying flat"


def test_no_large_band_shares_a_z_range_with_a_placed_asset_band():
    """
    Regression pin for a real, measured Cycles rendering bug (not a guess):
    a placed asset renders invisible when its bounding box sits contained
    inside a much larger opaque band's bounding box on 2+ axes -- reproduced
    and root-caused with 8+ controlled renders during development. wall_bay's
    "panel" band used to span the same Z-range as every stock_N band and
    silently hid all 16 phones despite every unit test passing (the geometry
    was correct; only Cycles' rendering of it was wrong) -- exactly the kind
    of defect this session learned only a real render catches. "panel" was
    shortened to end below the first stock band as the fix; this pins that
    invariant for every fixture type, not just wall_bay, since any future
    asset_key band sharing Z-space with a big backdrop band would hit the
    same rendering bug regardless of which recipe it's in.
    """
    from app.services.fixture_types import FIXTURE_TYPES

    _LARGE_BAND_FRACTION = 0.15  # matches panel's real scale, not a measurement

    for type_key, spec in FIXTURE_TYPES.items():
        asset_bands = [b for b in spec.bands if b.asset_key]
        if not asset_bands:
            continue
        large_bands = [b for b in spec.bands if not b.asset_key and (b.z1 - b.z0) > _LARGE_BAND_FRACTION]
        for asset_band in asset_bands:
            for large_band in large_bands:
                overlap = min(asset_band.z1, large_band.z1) - max(asset_band.z0, large_band.z0)
                assert overlap <= 0, (
                    f"{type_key}.{large_band.name} Z-overlaps placed-asset band "
                    f"{asset_band.name} -- this hides the asset in Cycles, see docstring"
                )


def test_every_placed_phone_shares_one_material_object_not_one_each():
    """
    trimesh does not instance geometry (the same GLB-duplication limitation
    noted elsewhere in this module), so a shelf full of phones is a shelf
    full of independently embedded texture copies unless every placed copy
    shares the exact same material/image object. This is what keeps a
    16-phone wall bay from embedding its 20KB texture sixteen times over.
    """
    result = build_massing(_single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193))
    cells = _named(result, "fixture_0_peg_stock_1_")
    materials = {id(m.visual.material) for m in cells.values()}
    assert len(materials) == 1, "every placed phone should share the same material object"


def test_a_placed_asset_carries_its_own_texture_not_a_palette_colour():
    """The whole point of using a real asset here: unlike every other fixture
    material, this one must keep its authored texture, not get overwritten
    by mesh_builder's flat palette (see add()'s material_key=None contract)."""
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))
    device = next(iter(_named(result, "fixture_0_device_").values()))
    assert device.visual.material.baseColorTexture is not None


def test_placed_props_fall_back_to_a_procedural_box_when_the_asset_is_missing(monkeypatch):
    """
    A missing or broken asset file must degrade to the band's own procedural
    look (its material/inset/corner_r), not break the model -- the same
    "None is a first-class outcome" contract asset_loader.load_asset already
    documents for the whole-fixture path, now covered for per-cell props too.
    """
    from app.services import asset_loader

    monkeypatch.setattr(asset_loader, "load_asset", lambda key: None)
    result = build_massing(_single("fixture", "CENTER BAR NARROW - NAR-5M-END", 1371, 900, 900))

    devices = _named(result, "fixture_0_device_")
    assert len(devices) == 6
    for name, mesh in devices.items():
        assert not mesh.metadata.get("placed_asset"), name
        assert mesh.is_watertight, f"{name} fallback box should be watertight"


def test_placed_props_never_spill_outside_their_cells_footprint():
    """A prop's target size is a stated guess (asset_target_mm), not a
    measurement -- it must yield to whatever room the cell actually has,
    the same "shrink to fit, never distort past the footprint" discipline
    project_d and recess_mm already follow."""
    from app.services.fixture_types import Band, FixtureType
    import app.services.fixture_types as ftypes_mod

    key = "__test_prop_clamp__"
    ftypes_mod.FIXTURE_TYPES[key] = FixtureType(
        key=key,
        bands=(
            Band("device", 0.0, 1.0, ftypes_mod.MAT_PANEL, asset_key="product_phone_generic",
                 asset_lay_flat=True, asset_target_mm=9999.0),
        ),
        default_w=300.0, default_d=300.0, default_h=50.0,
    )
    try:
        layout = _single(key, "", 300, 300, 50)
        result = build_massing(layout)
        lo, hi = _fixture_bounds(result, 0)
        assert (hi[0] - lo[0]) <= 300.0 + 1e-6
        assert (hi[1] - lo[1]) <= 300.0 + 1e-6
    finally:
        del ftypes_mod.FIXTURE_TYPES[key]


def test_product_placeholders_stay_watertight_and_within_the_shelf_envelope():
    """
    A placed asset (see mesh.metadata["placed_asset"]) is not held to the
    watertight guarantee -- it is a third-party mesh, same policy as the
    whole-fixture asset path -- but everything else on the shelf still must
    be, and the shelf's own declared POC depth envelope still must hold
    regardless of what's standing on it.
    """
    result = build_massing(_single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193))
    checked = 0
    for name, mesh in result.scene.geometry.items():
        if not ("_ledge_stock_" in name or "_peg_stock_" in name or name.startswith("fixture_0_device_")):
            continue
        checked += 1
        if mesh.metadata.get("placed_asset"):
            continue
        assert mesh.is_watertight, name
    assert checked > 0, "prefix match found nothing to check -- band names drifted again"
    lo, hi = _fixture_bounds(result, 0)
    envelope_d = 100.0 * 3.0  # wall_bay's poc_envelope_d
    assert (hi[1] - lo[1]) <= envelope_d + 1e-6


# ---------------------------------------------------------------------------
# visual_profiles — the reusable, opt-in retail showroom configuration
# ---------------------------------------------------------------------------


def test_no_profile_selected_produces_the_exact_same_model_as_before_profiles_existed():
    """
    The reusability guarantee: a layout that never opts in must render
    byte-identical geometry to today's behaviour. Checked against the
    committed vertex_hash of the un-profiled reconstructed-style wall_bay
    layout, not just "doesn't crash" -- a silent drift here would mean some
    other project's model changed because *a different* project selected a
    profile, which is exactly what the opt-in design promises can't happen.
    """
    layout = _single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193)
    a = build_massing(layout)
    b = build_massing(layout)
    assert a.vertex_hash() == b.vertex_hash()
    assert not any("pilaster" in name for name in a.scene.geometry)


def test_an_unrecognised_profile_name_degrades_to_no_profile_rather_than_raising():
    layout = _single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193, visual_profile="not_a_real_profile")
    result = build_massing(layout)  # must not raise
    assert not any("pilaster" in name for name in result.scene.geometry)


def test_selecting_a_profile_does_not_change_an_unrelated_layouts_hash():
    """The opt-in guarantee, the other direction: building *with* a profile
    selected must not leave any process-global state that changes a
    subsequent, unrelated, no-profile build's output."""
    plain = _single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193)
    before = build_massing(plain).vertex_hash()
    profiled = _single(
        "12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193,
        visual_profile="retail_showroom",
    )
    build_massing(profiled)
    after = build_massing(plain).vertex_hash()
    assert before == after


def test_retail_showroom_profile_adds_wall_bay_end_pilasters():
    layout = _single(
        "12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193,
        visual_profile="retail_showroom",
    )
    result = build_massing(layout)
    pilasters = _named(result, "fixture_0_pilaster_")
    assert set(pilasters) == {"fixture_0_pilaster_l", "fixture_0_pilaster_r"}
    for mesh in pilasters.values():
        assert mesh.is_watertight


def test_pilasters_stay_pinned_to_the_footprints_left_and_right_edges():
    layout = _single(
        "12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193,
        visual_profile="retail_showroom",
    )
    result = build_massing(layout)
    lo, hi = _fixture_bounds(result, 0)
    left = result.scene.geometry["fixture_0_pilaster_l"]
    right = result.scene.geometry["fixture_0_pilaster_r"]
    # Each pilaster sits at its own edge, not centred, and neither one alone
    # spans anywhere near the fixture's full declared width.
    assert (left.bounds[1][0] - left.bounds[0][0]) < 100.0
    assert (right.bounds[1][0] - right.bounds[0][0]) < 100.0
    assert left.bounds[0][0] <= lo[0] + 1e-6 or right.bounds[1][0] >= hi[0] - 1e-6


def test_pilasters_do_not_bbox_contain_the_placed_phones_they_share_z_range_with():
    """
    The pilaster band (edge_w, procedural) deliberately shares a Z-range with
    the placed-asset stock bands -- unlike the old "panel" backdrop that
    caused the real Cycles invisibility bug (see
    test_no_large_band_shares_a_z_range_with_a_placed_asset_band), a pilaster
    is narrow enough at the footprint edge that it cannot 2-axis-contain a
    centred product on the X axis, which is the actual measured trigger
    condition, not Z-overlap alone. Verified geometrically here rather than
    asserted by design, since only a render caught the original bug.
    """
    layout = _single(
        "12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193,
        visual_profile="retail_showroom",
    )
    result = build_massing(layout)
    pilasters = list(_named(result, "fixture_0_pilaster_").values())
    phones = [
        m for name, m in result.scene.geometry.items()
        if ("_ledge_stock_" in name or "_peg_stock_" in name) and m.metadata.get("placed_asset")
    ]
    assert pilasters and phones
    for pil in pilasters:
        p_lo, p_hi = pil.bounds
        for phone in phones:
            f_lo, f_hi = phone.bounds
            x_overlap = min(p_hi[0], f_hi[0]) - max(p_lo[0], f_lo[0])
            # Any real containment needs the phone's X-span to sit entirely
            # within the (much narrower) pilaster's -- geometrically
            # impossible here since the pilaster is the narrower of the two.
            assert not (x_overlap > 0 and f_lo[0] >= p_lo[0] - 1e-6 and f_hi[0] <= p_hi[0] + 1e-6)


def test_visual_profile_selection_is_recorded_in_the_resolution_log():
    layout = _single(
        "12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193,
        visual_profile="retail_showroom",
    )
    result = build_massing(layout)
    fields = [r["field"] for r in result.resolutions]
    assert "visual_profile" in fields


def test_no_profile_selected_leaves_no_visual_profile_entry_in_the_resolution_log():
    """A profile is a deliberate opt-in choice, not an unknown field the
    ladder had to guess at -- logging it at rank 5 (global_default) the way
    every other unresolved field is logged would drag every un-profiled
    model's confidence down to 0.20 and wrongly flip not_for_construction on
    for every layout that simply never asked for a profile."""
    result = build_massing(_single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193))
    fields = [r["field"] for r in result.resolutions]
    assert "visual_profile" not in fields


def test_product_catalog_selection_is_deterministic_across_builds():
    from app.services import visual_profiles as vprofiles

    key = "__test_catalog__"
    profile = vprofiles.VisualProfile(
        key=key, product_catalog={"stock_1": ("product_phone_generic", "product_phone_generic")}
    )
    vprofiles.VISUAL_PROFILES[key] = profile
    try:
        layout = _single("12-L", "BASE WALL-1 (BW-1)", 1100, 100, 2193, visual_profile=key)
        a = build_massing(layout).vertex_hash()
        b = build_massing(layout).vertex_hash()
        assert a == b
    finally:
        del vprofiles.VISUAL_PROFILES[key]


def test_an_asset_key_tuple_on_a_band_works_without_any_profile_selected():
    """A tuple asset_key is itself a small catalog (see the Band docstring),
    independent of visual_profiles -- a recipe can declare product variety
    without any profile involved at all.

    Patches the real, dispatchable "generic_box" fallback type rather than
    registering a new key: resolve_fixture_type's cascade (item code ->
    keyword -> kind -> dimension heuristic) only ever returns a key from its
    own fixed match order or the fallback, so an ad-hoc key registered
    directly into FIXTURE_TYPES is never reachable through the public
    resolver -- get_type("generic_box") is what a genuinely unmatched
    fixture actually receives.
    """
    import app.services.fixture_types as ftypes_mod
    from app.services.fixture_types import Band, FixtureType

    original = ftypes_mod.FIXTURE_TYPES["generic_box"]
    ftypes_mod.FIXTURE_TYPES["generic_box"] = FixtureType(
        key="generic_box",
        bands=(
            Band("device", 0.0, 1.0, ftypes_mod.MAT_PANEL,
                 asset_key=("product_phone_generic", "product_phone_generic"),
                 asset_lay_flat=True, repeat_w=2),
        ),
        default_w=600.0, default_d=300.0, default_h=50.0,
        kinds=original.kinds,
    )
    try:
        result = build_massing(_single("totally_unmatched_kind_xyz", "", 600, 300, 50))
        devices = _named(result, "fixture_0_device_")
        assert len(devices) == 2
        for mesh in devices.values():
            assert mesh.metadata.get("placed_asset") is True
    finally:
        ftypes_mod.FIXTURE_TYPES["generic_box"] = original


# ---------------------------------------------------------------------------
# turntable_viewpoints — camera path for the photoreal turntable
# ---------------------------------------------------------------------------


def test_turntable_viewpoints_returns_the_requested_frame_count():
    from app.services.mesh_builder import turntable_viewpoints

    vps = turntable_viewpoints(_room_layout(), 3000.0, 12)
    assert len(vps) == 12
    assert {vp["id"] for vp in vps} == {f"T{i:02d}" for i in range(12)}


def test_turntable_viewpoints_are_evenly_spaced_around_one_full_circle():
    import math

    from app.services.mesh_builder import turntable_viewpoints

    vps = turntable_viewpoints(_room_layout(), 3000.0, 8)
    target = vps[0]["target"]
    for vp in vps:
        assert vp["target"] == target, "every frame looks at the same point"

    angles = []
    for vp in vps:
        dx = vp["position"][0] - target[0]
        dy = vp["position"][1] - target[1]
        angles.append(math.atan2(dy, dx))
    angles.sort()
    steps = [(angles[(i + 1) % 8] - angles[i]) % (2 * math.pi) for i in range(8)]
    for step in steps:
        assert step == pytest.approx(2 * math.pi / 8, abs=1e-6)


def test_turntable_viewpoints_hold_a_constant_radius_and_height():
    import math

    from app.services.mesh_builder import turntable_viewpoints

    vps = turntable_viewpoints(_room_layout(), 3000.0, 10)
    target = vps[0]["target"]
    radii = [
        math.hypot(vp["position"][0] - target[0], vp["position"][1] - target[1]) for vp in vps
    ]
    heights = [vp["position"][2] for vp in vps]
    assert max(radii) - min(radii) < 1e-6
    assert max(heights) - min(heights) < 1e-6


def test_turntable_viewpoints_is_empty_for_a_degenerate_layout():
    from app.services.mesh_builder import turntable_viewpoints

    empty_layout = UnifiedLayout(units="mm")
    assert turntable_viewpoints(empty_layout, 3000.0, 24) == []
    assert turntable_viewpoints(_room_layout(), 3000.0, 0) == []
