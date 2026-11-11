"""
Generic retail fixture taxonomy and type resolution.

Deliberately company-neutral: these are shapes a retail interior is made of, not
any vendor's product line. Vendor item codes and descriptions map *into* this
taxonomy; no brand names, geometry or branding live here.

Why types at all
    A massing model where a wall bay, a cash counter and a display panel are all
    the same grey box is dimensionally correct and visually useless. Giving each
    type a recipe — a small stack of bands with its own shade — is what makes a
    plan read as a shop rather than a bar chart, and it costs no authored assets.

The vertical band module
    Retail wall runs are a fixed section: a dark base cabinet to roughly 900mm, a
    lighter backpanel above it carrying product, and a boxing band at the top.
    Rendering those three bands instead of one box is the single largest visual
    gain available from the data we already hold.

Two key spaces
    The two ingest paths produce completely different fixture keys:

        BOQ workbook   kind == label == item code   e.g. "13-A"
        plan PDF/CSV   kind == category             e.g. "display"
                       label == description         e.g. '65" Smart Signage'

    so ``resolve_fixture_type`` tries item code, then description keyword, then
    kind keyword, then a dimensional heuristic, before giving up to a plain box.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

# ---------------------------------------------------------------------------
# Band recipes
# ---------------------------------------------------------------------------

# Material keys — resolved to colours in mesh_builder's palette.
MAT_BODY = "body"          # dark cabinetry
MAT_TOP = "top"            # worktop / counter surface
MAT_PANEL = "panel"        # light backpanel, product zone
MAT_BOXING = "boxing"      # upper boxing / bulkhead band
MAT_SCREEN = "screen"      # dark inset screen face
MAT_ACCENT = "accent"      # signage, logos
MAT_GENERIC = "generic"    # untyped fallback
MAT_EMISSIVE = "emissive"  # backlit / illuminated panels


@dataclass(frozen=True)
class Band:
    """
    One horizontal slice of a fixture.

    ``z0``/``z1`` are fractions of the fixture's total height, so a recipe works
    at any declared size. ``inset_d`` shrinks the band in depth (as a fraction of
    the footprint depth) so a backpanel can sit behind a cabinet; ``inset_w``
    does the same across the width.

    Bands shrink by default, and may project forward only in depth, only by
    declaring ``project_d``, and only within the type's ``poc_envelope_d``. A
    worktop that overhangs sideways would push the model's X/Y bounds past the
    layout's for no visual gain, so width still only ever shrinks and the
    overhang read comes from insetting the body beneath instead.
    """

    name: str
    z0: float
    z1: float
    material: str = MAT_BODY
    inset_w: float = 0.0
    inset_d: float = 0.0
    back: bool = False  # sit the band toward the rear of the footprint
    # Depth projected forward of the declared footprint, as a fraction of it.
    #
    # POC ONLY, and the reason it exists is narrow. A BOQ gives the *product*:
    # BW-1 is a 1100 x 100 x 2193mm wall panel, and 100mm is the panel, honestly
    # stated. The installed bay is that panel plus shelves standing out in front
    # of it, so a fixture drawn strictly inside 100mm can only ever be a flat
    # slab -- which is exactly what it looked like. A projecting band buys the
    # shelves back without touching the BOQ figure, which stays authoritative.
    #
    # A band that projects is anchored to the rear face by definition: it grows
    # into the room, never backwards through the wall behind it.
    project_d: float = 0.0
    # Instance this band as a grid of copies, evenly spread across the
    # fixture's width and depth. 1 x 1 is a single centred band, which is what
    # every recipe had before this existed.
    #
    # This is the primitive the POC was missing, and its absence is why the
    # showroom rendered empty: a band is always centred, so there was no way to
    # put six phones on a table or four boxes on a shelf, however many bands a
    # recipe declared. Spacing is (i + 0.5) / n of the span, so copies are
    # evenly distributed with a half-gap at each end and never touch the edge.
    #
    # Grid copies sit inside the same footprint as a single band would, so the
    # width and envelope rules above still hold with nothing extra to check.
    repeat_w: int = 1
    repeat_d: int = 1
    # Corner radius as a fraction of the band's shorter footprint side, so it
    # scales with the fixture instead of being a fixed millimetre value that
    # looks right at one size only. Retail display tables are radiused, and a
    # sharp-cornered box is one of the tells that reads as "untreated massing"
    # at a glance. Rounding only ever removes material from the corners, so the
    # shrink-only rule above still holds and the bounding box is unchanged.
    corner_r: float = 0.0
    # Chamfer on the top edge, in mm: the top face insets by this much on every
    # side over the band's own height, so the band renders as a truncated
    # pyramid rather than a hard-edged slab. Worktops and boxing lips in the
    # reference proposal catch a highlight along a beveled top edge; a flat 90
    # degree box edge is one of the clearest "untreated massing" tells at
    # close range, the same way a sharp corner is for corner_r. Mutually
    # exclusive with corner_r in practice (no recipe sets both) -- combining a
    # radius with a taper needs a fancier mesh than this POC builds.
    bevel_mm: float = 0.0
    # Recess this band's front face this many mm behind where it would
    # otherwise sit, reading as glass set back from its frame or a groove
    # pressed into a drawer front. Clamped to the room already freed up by
    # inset_d, so it can only ever move material inward -- never past the
    # fixture's own declared footprint, and never through the fixture from the
    # other side. Meaningless (ignored) on a "back" or projecting band, which
    # already have their own depth placement.
    recess_mm: float = 0.0
    # Place a shared authored asset (see asset_loader.py) in every grid cell
    # instead of a procedural box -- product on a shelf or table, not a
    # fixture part. Two co-registered bands (a plain box plus one with
    # asset_key set, same repeat_w/repeat_d) are how a table or shelf gets
    # populated with real geometry while everything else about the recipe
    # stays untouched; see island_table's device band for the pattern.
    #
    # A tuple instead of a single key is a small product *catalog* for this
    # band: each grid cell picks one entry deterministically (by its own
    # cell index), so a shelf can carry a mix of product types instead of
    # sixteen copies of the same phone. A visual profile
    # (see visual_profiles.py) can widen the catalog per band name without
    # touching the recipe itself; a bare string is exactly the single-catalog
    # case and is what every existing recipe still declares.
    #
    # POC ONLY, and deliberately narrow: this is not a general "place any
    # mesh here" mechanism, and it does not go through fit_asset's
    # refuse-rather-than-distort ladder -- there is no declared size for
    # "one phone on a shelf" to refuse against. It is scaled to
    # asset_target_mm and clamped to the cell, then falls back to a plain
    # box (this band's own inset_w/inset_d/corner_r) if the asset fails to
    # load, so a missing file degrades instead of breaking the model.
    asset_key: str | tuple[str, ...] | None = None
    # Target size in mm for the asset's longest dimension before the
    # per-cell clamp. A stated guess, not a measurement -- there is no
    # source data for "how big is generic shelf product," only a size that
    # reads as plausible next to the fixture it sits in.
    asset_target_mm: float = 140.0
    # Tip the asset from its authored standing orientation onto its back
    # (screen facing up) -- for product resting flat on a table rather than
    # standing upright on a shelf.
    asset_lay_flat: bool = False
    # Recline the asset back by this many degrees from fully upright (0) --
    # the same tilt a real product-display stand gives a phone, and why one
    # is needed at all: a perfectly vertical flat object viewed from any
    # oblique angle (every camera position this pipeline actually uses
    # except a face-on elevation) foreshortens to a thin edge-on sliver,
    # reading as a pin rather than a product (measured by rendering and
    # looking, not guessed -- see PLANFORGE.md/the session's own render QA
    # notes). Ignored when asset_lay_flat is set (already fully reclined).
    asset_tilt_deg: float = 0.0
    # Render this band as two narrow copies pinned to the footprint's left and
    # right edges (each edge_w mm wide) instead of one centred copy -- a wall
    # bay's end pilasters, which repeat_w cannot produce: a repeat grid always
    # spreads copies as *evenly spaced cells across the whole span*, so the
    # narrowest setting it can express is one strip down the middle, not two
    # at the edges (checked; it renders as an off-centre mullion, not a
    # frame). Mutually exclusive with repeat_w/repeat_d in practice -- no
    # recipe sets both. 0 (default) is today's exact single-band behaviour.
    edge_w: float = 0.0


@dataclass(frozen=True)
class FixtureType:
    """A recipe plus the defaults used when the source omits a dimension."""

    key: str
    bands: tuple[Band, ...]
    default_w: float
    default_d: float
    default_h: float
    default_elevation: float = 0.0
    needs_asset: bool = False
    # The total depth this type's rendered geometry may occupy, as a multiple of
    # the declared footprint depth. 1.0 means "never leaves the footprint",
    # which is every type except the wall-mounted shelving that needs shelves.
    #
    # POC ONLY. This is a rendering allowance, not a claim about the real
    # fixture and not an input anyone supplies -- it deliberately is not a
    # schema field, so nothing downstream can mistake it for surveyed data. It
    # exists so that "bands never leave the footprint" can be replaced by a
    # bound that is still explicit, still tested, and still refuses to let a
    # recipe grow without saying so. A real installed-depth concept, resolved
    # from client data with its own provenance, is a later and separate job.
    poc_envelope_d: float = 1.0
    # What a source's height_mm measures for this type. Retail schedules quote
    # *seat* height for seating and overall height for everything else; comparing
    # the two wrongly rejects a perfectly good asset.
    height_means: str = "overall"
    item_codes: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()

    @property
    def band_count(self) -> int:
        return len(self.bands)


_SOLID = (Band("body", 0.0, 1.0, MAT_BODY),)


# ---------------------------------------------------------------------------
# The taxonomy
# ---------------------------------------------------------------------------
#
# Item codes are from the sample BOQ and are treated as opaque strings; they
# carry no brand meaning here. Keywords are matched case-insensitively as
# substrings, in table order, first match wins.

FIXTURE_TYPES: dict[str, FixtureType] = {
    "base_cabinet": FixtureType(
        key="base_cabinet",
        # Toe-kick, drawer fronts, worktop. The recessed kick at the bottom is
        # what stops a cabinet run reading as one extruded slab: it puts a
        # shadow line along the floor the way real casework does.
        bands=(
            Band("kick", 0.00, 0.11, MAT_BODY, inset_w=0.06, inset_d=0.30),
            Band("body", 0.11, 0.94, MAT_BODY, inset_w=0.03, inset_d=0.08),
            Band("drawer", 0.16, 0.50, MAT_BOXING, inset_w=0.08, inset_d=0.04),
            Band("drawer_upper", 0.55, 0.89, MAT_BOXING, inset_w=0.08, inset_d=0.04),
            # Handleless-cabinetry finger-pull grooves, recessed just under each
            # drawer front's own top edge. A flush drawer front with nothing to
            # grip is a tell in itself; a continuous shadow groove is the
            # minimalist-fixture answer real casework uses instead of a pull.
            Band("handle_lower", 0.455, 0.485, MAT_BOXING, inset_w=0.12, inset_d=0.18, recess_mm=5.0),
            Band("handle_upper", 0.845, 0.875, MAT_BOXING, inset_w=0.12, inset_d=0.18, recess_mm=5.0),
            Band("top", 0.94, 1.00, MAT_TOP, bevel_mm=6.0),
        ),
        default_w=1100.0, default_d=320.0, default_h=900.0,
        item_codes=("13-A", "13-B", "13-C", "13-D"),
        keywords=("base cabinet", "bc-"),
        kinds=("cabinet",),
    ),
    "wall_bay": FixtureType(
        key="wall_bay",
        # A retail bay is a thin lit backdrop with shelves standing out in front
        # of it, carrying product. Drawn strictly inside the BOQ's 100mm panel
        # depth it can only be a flat slab, so the shelves project -- see
        # Band.project_d. Shelf pitch and count are the POC's own assumption:
        # the BOQ describes one panel, never how it is shelved.
        # Reconstructed against the reference proposal's own wall-bay photos
        # (Views 2-3): a shallow *ledge* just above the base cabinet, carrying
        # a few larger standing devices, topped by several rows of a dense,
        # near-flush *pegboard* zone -- many small products mounted close to
        # the backpanel, not standing out on deep shelf boards. The old
        # 4-shelf recipe used one uniform "deep shelf" language for the whole
        # bay, which read as generic shelving rather than merchandised
        # retail wall -- rebuilt to visually separate the two zones the way
        # the reference actually does.
        bands=(
            Band("base", 0.00, 0.41, MAT_BODY),
            # Stops at 0.46, short of the boxing at the top (0.88) -- see
            # "Placed-asset rendering note" below for why. Real usage is
            # wall-mounted anyway, so the actual wall behind the fixture
            # keeps the backdrop visually continuous above this line; this
            # short band still reads as a proper plinth/backdrop transition
            # right above the base cabinet.
            Band("panel", 0.41, 0.46, MAT_PANEL, inset_d=0.35, back=True),
            # The ledge: a real shelf board, fewer and larger standing
            # devices -- the reference's wood-tone ledge row.
            Band("ledge_shelf", 0.465, 0.485, MAT_BODY, inset_w=0.04, back=True, project_d=1.5),
            # See "Placed-asset rendering note" below for why this and every
            # stock/ledge band stays clear of a large non-asset band's Z-range.
            Band("ledge_stock", 0.485, 0.545, MAT_PANEL, inset_w=0.10, back=True,
                 project_d=1.3, repeat_w=3, corner_r=0.28,
                 asset_key="product_phone_generic", asset_target_mm=170.0,
                 asset_tilt_deg=18.0),
            # The pegboard: five tight rows of small product mounted close to
            # flush (project_d well under the ledge's), reading as hung
            # merchandise rather than shelving. Row boards are thin rails,
            # not shelf-depth boxes -- the product density is what carries
            # the read, not the board underneath it.
            Band("peg_shelf_1", 0.560, 0.566, MAT_BODY, inset_w=0.04, back=True, project_d=0.5),
            Band("peg_stock_1", 0.566, 0.611, MAT_PANEL, inset_w=0.14, back=True,
                 project_d=0.35, repeat_w=6, corner_r=0.22,
                 asset_key="product_phone_generic", asset_target_mm=100.0,
                 asset_tilt_deg=12.0),
            Band("peg_shelf_2", 0.6185, 0.6245, MAT_BODY, inset_w=0.04, back=True, project_d=0.5),
            Band("peg_stock_2", 0.6245, 0.6695, MAT_PANEL, inset_w=0.14, back=True,
                 project_d=0.35, repeat_w=6, corner_r=0.22,
                 asset_key="product_phone_generic", asset_target_mm=100.0,
                 asset_tilt_deg=12.0),
            Band("peg_shelf_3", 0.677, 0.683, MAT_BODY, inset_w=0.04, back=True, project_d=0.5),
            Band("peg_stock_3", 0.683, 0.728, MAT_PANEL, inset_w=0.14, back=True,
                 project_d=0.35, repeat_w=6, corner_r=0.22,
                 asset_key="product_phone_generic", asset_target_mm=100.0,
                 asset_tilt_deg=12.0),
            Band("peg_shelf_4", 0.7355, 0.7415, MAT_BODY, inset_w=0.04, back=True, project_d=0.5),
            Band("peg_stock_4", 0.7415, 0.7865, MAT_PANEL, inset_w=0.14, back=True,
                 project_d=0.35, repeat_w=6, corner_r=0.22,
                 asset_key="product_phone_generic", asset_target_mm=100.0,
                 asset_tilt_deg=12.0),
            Band("peg_shelf_5", 0.794, 0.800, MAT_BODY, inset_w=0.04, back=True, project_d=0.5),
            # Product standing on each deck. Shelves with nothing on them read
            # as empty racking; this is what makes a bay look merchandised.
            #
            # A real placed asset (see asset_loader.py / manifest.json), not a
            # procedural block -- a shared, de-branded generic-phone mesh,
            # standing in its natural upright orientation (asset_lay_flat
            # unset), scaled to asset_target_mm and clamped to its cell.
            # inset_w/corner_r/MAT_PANEL below are the fallback only, used if
            # the asset fails to load -- same rounded-body look the asset
            # itself replaces when present.
            #
            # Placed-asset rendering note: a placed asset's band must not
            # share a Z-range with "panel" above. Measured (not guessed):
            # Cycles renders a placed asset invisible when its bounding box
            # sits 2-or-more-axis-contained inside a much larger opaque
            # object's bounding box (here, panel at ~15-70x the phone's own
            # size) -- reproduced and root-caused with 8+ controlled test
            # renders during development (see commit history), not a
            # one-off. Breaking containment on any one axis (here, Z, by
            # keeping panel below every stock band) fixes it. Procedural
            # (non-asset) bands are unaffected by this -- every shelf/ledge/
            # peg_shelf band overlaps panel's old Z-range constantly (and, in
            # the retail_showroom profile, the pilaster's) and always
            # renders fine; only a band sharing Z-range with a placed asset
            # AND large enough to contain it on 2+ axes is at risk, which is
            # exactly what every shelf/rail band here is built narrow/thin
            # enough to avoid (pinned generically for every fixture type by
            # test_no_large_band_shares_a_z_range_with_a_placed_asset_band).
            Band("peg_stock_5", 0.800, 0.845, MAT_PANEL, inset_w=0.14, back=True,
                 project_d=0.35, repeat_w=6, corner_r=0.22,
                 asset_key="product_phone_generic", asset_target_mm=100.0,
                 asset_tilt_deg=12.0),
            # No pilasters here, though the reference has them. A band is always
            # centred on its fixture, so an inset_w narrow enough to read as an
            # upright renders one mullion down the middle of the bay instead of
            # two at its edges -- checked, and it looked like a defect rather
            # than a frame. Edge uprights need a width offset the band system
            # does not have (see Band.edge_w, added for exactly this, applied
            # via the retail_showroom visual profile rather than here
            # -- see visual_profiles.py).
            Band("boxing", 0.88, 1.00, MAT_BOXING, inset_d=0.15, back=True, project_d=1.2, bevel_mm=6.0),
        ),
        default_w=1100.0, default_d=100.0, default_h=2200.0,
        # 100mm panel + ~200mm of shelf = 300mm of room taken, which is an
        # ordinary retail bay depth and the shallowest that reads as shelved.
        poc_envelope_d=3.0,
        item_codes=("12-L",),
        keywords=("base wall", "bw-1", "wall bay"),
        kinds=("wall_bay",),
    ),
    "display_panel": FixtureType(
        key="display_panel",
        bands=(
            Band("frame", 0.00, 1.00, MAT_PANEL),
            # Recessed a little behind the frame, like real glass set into a
            # bezel -- flush-mounted, the face and frame read as one band with
            # a colour change rather than as two physically distinct parts.
            Band("face", 0.08, 0.92, MAT_SCREEN, inset_w=0.06, inset_d=0.25, recess_mm=8.0),
        ),
        default_w=1100.0, default_d=100.0, default_h=1300.0,
        default_elevation=900.0,
        item_codes=("12-P", "12-D", "12-E"),
        keywords=("flat panel", "accessory flat panel", "fp-", "np-", "lfd", "signage panel"),
        kinds=("display",),
    ),
    "island_table": FixtureType(
        key="island_table",
        # A demo table is read by what is on it. Bare, it is a white lid on a
        # dark box and could be any furniture; the phones lying on their mats
        # are what make it a phone shop. Product occupies the top ~60mm of the
        # declared height rather than standing above it, so the fixture still
        # ends exactly where the BOQ says it does.
        bands=(
            # Rounded, not a sharp-cornered slab -- the reference's black
            # table bases read as one continuous rounded-rectangle form, top
            # to bottom, not a rounded lid on a square box.
            Band("plinth", 0.00, 0.86, MAT_BODY, inset_w=0.14, inset_d=0.18, corner_r=0.06),
            Band("top", 0.86, 0.93, MAT_TOP, corner_r=0.10),
            Band("mat", 0.93, 0.955, MAT_SCREEN, inset_w=0.30, inset_d=0.34,
                 repeat_w=3, repeat_d=2),
            # A real placed asset. Standing in its authored upright pose, not
            # tipped flat -- the reference's demo tables show product upright
            # on small display stands/cards, not lying down (checked against
            # the reference's own table photos; a lay-flat phone read as
            # "product laid down for stocktaking," not "on display"). A
            # catalog entry normalised native_pose="resting" (a laptop -- see
            # asset_loader.native_pose) is unaffected by this and keeps its
            # own correct open-on-the-table pose either way.
            Band("device", 0.955, 1.00, MAT_PANEL, inset_w=0.46, inset_d=0.52,
                 repeat_w=3, repeat_d=2, corner_r=0.32,
                 asset_key="product_phone_generic",
                 asset_target_mm=150.0, asset_tilt_deg=22.0),
        ),
        default_w=1400.0, default_d=900.0, default_h=900.0,
        item_codes=(),
        keywords=("center bar", "centre bar", "nar-", "island", "demo table",
                  "training table", "accessory table"),
        kinds=("table",),
    ),
    "counter": FixtureType(
        key="counter",
        bands=(
            Band("body", 0.00, 0.92, MAT_BODY, inset_w=0.10, inset_d=0.14),
            Band("top", 0.92, 1.00, MAT_TOP, corner_r=0.08),
        ),
        default_w=1600.0, default_d=700.0, default_h=950.0,
        keywords=("counter", "cashier", "checkout"),
        kinds=("counter",),
    ),
    "service_desk": FixtureType(
        key="service_desk",
        # A desk people stand at: recessed plinth, a lighter front panel facing
        # the customer, worktop, and a screen standing on it. Without the
        # screen it reads as a counter rather than a service point.
        bands=(
            Band("plinth", 0.00, 0.12, MAT_BODY, inset_w=0.16, inset_d=0.24),
            Band("body", 0.12, 0.82, MAT_BODY, inset_w=0.12, inset_d=0.16),
            Band("front", 0.20, 0.78, MAT_PANEL, inset_w=0.18, inset_d=0.06),
            Band("top", 0.82, 0.90, MAT_TOP, corner_r=0.08),
            Band("screen", 0.90, 1.00, MAT_SCREEN, inset_w=0.62, inset_d=0.72, recess_mm=6.0),
        ),
        default_w=1600.0, default_d=800.0, default_h=1100.0,
        item_codes=("9-B",),
        keywords=("customer service", "service desk", "cs-2", "cs-2p"),
        kinds=("desk",),
    ),
    "shelf_unit": FixtureType(
        key="shelf_unit",
        # Open shelving with stock on it. Empty shelves read as a bookcase, so
        # each deck carries a row of boxes; the plinth lifts it off the floor.
        bands=(
            Band("back", 0.00, 1.00, MAT_PANEL, inset_d=0.55, back=True),
            Band("plinth", 0.00, 0.08, MAT_BODY, inset_w=0.05, inset_d=0.20),
            Band("shelf_1", 0.30, 0.34, MAT_BODY),
            Band("shelf_2", 0.55, 0.59, MAT_BODY),
            Band("shelf_3", 0.80, 0.84, MAT_BODY),
            Band("stock_1", 0.34, 0.52, MAT_BOXING, inset_w=0.14, inset_d=0.46, repeat_w=3),
            Band("stock_2", 0.59, 0.77, MAT_BOXING, inset_w=0.14, inset_d=0.46, repeat_w=3),
            Band("stock_3", 0.84, 0.98, MAT_BOXING, inset_w=0.14, inset_d=0.46, repeat_w=3),
        ),
        default_w=1000.0, default_d=350.0, default_h=2100.0,
        keywords=("shelf", "shelving", "accessor"),
        kinds=("shelving",),
    ),
    "screen_wall": FixtureType(
        key="screen_wall",
        bands=(
            Band("frame", 0.00, 1.00, MAT_BODY),
            # The reference's brand wall reads as a 2x2 tiled video wall (a
            # real LFD installation is built from a grid of panels, and the
            # seams between them are part of the read, not a flaw) -- two
            # stacked bands, each split left/right by repeat_w, in place of
            # one single glass face. The biggest glass in the taxonomy, so it
            # keeps the deepest recess: real LFD panels sit a visible amount
            # behind their bezel, more than a small display's thin frame would.
            Band("screen_upper", 0.55, 0.94, MAT_SCREEN, inset_w=0.07, inset_d=0.18,
                 recess_mm=12.0, repeat_w=2),
            Band("screen_lower", 0.16, 0.53, MAT_SCREEN, inset_w=0.07, inset_d=0.18,
                 recess_mm=12.0, repeat_w=2),
        ),
        default_w=2100.0, default_d=155.0, default_h=2280.0,
        item_codes=("14-A",),
        keywords=("brand wall", "video wall", "screen wall"),
        kinds=(),
    ),
    "boxing": FixtureType(
        key="boxing",
        # A beveled top edge instead of the plain _SOLID box: this is the
        # bulkhead cap right under the ceiling, one of the largest flat slabs
        # in the whole model, and the reference proposal's boxing lips clearly
        # catch a highlight along a chamfered top edge rather than a hard 90.
        bands=(Band("body", 0.0, 1.0, MAT_BODY, bevel_mm=8.0),),
        default_w=2100.0, default_d=155.0, default_h=700.0,
        default_elevation=2200.0,
        item_codes=("14-B",),
        keywords=("top boxing", "bw-top", "boxing"),
        kinds=(),
    ),
    "signage_panel": FixtureType(
        key="signage_panel",
        bands=(Band("face", 0.0, 1.0, MAT_ACCENT),),
        default_w=950.0, default_d=30.0, default_h=200.0,
        default_elevation=2200.0,
        item_codes=("31-A",),
        keywords=("signage", "logo"),
        kinds=(),
    ),
    "light_box": FixtureType(
        key="light_box",
        bands=(
            Band("frame", 0.00, 1.00, MAT_BOXING),
            # Inset face reads as backlit acrylic without any texture work —
            # the emissive material carries the glow, not a lightmap. A small
            # recess behind the frame sells the "lit panel behind a bezel"
            # read the same way it does for an unlit screen.
            Band("face", 0.10, 0.90, MAT_EMISSIVE, inset_w=0.08, inset_d=0.30, recess_mm=4.0),
        ),
        default_w=1200.0, default_d=120.0, default_h=400.0,
        default_elevation=2200.0,
        keywords=("light box", "lightbox", "isb light box"),
        kinds=("light_box",),
    ),
    "pillar": FixtureType(
        key="pillar",
        bands=(Band("body", 0.0, 1.0, MAT_PANEL),),
        default_w=300.0, default_d=300.0, default_h=3000.0,
        needs_asset=True,
        item_codes=("21-B",),
        keywords=("pillar", "column"),
        kinds=(),
    ),
    "partition": FixtureType(
        key="partition",
        bands=(Band("body", 0.0, 1.0, MAT_PANEL),),
        default_w=2000.0, default_d=100.0, default_h=3000.0,
        item_codes=(),
        keywords=("wall paneling", "partition", "paneling"),
        kinds=(),
    ),
    "chair": FixtureType(
        key="chair",
        bands=(
            Band("legs", 0.00, 0.52, MAT_BODY, inset_w=0.45, inset_d=0.45),
            Band("seat", 0.52, 0.62, MAT_TOP),
            Band("back", 0.62, 1.00, MAT_BODY, inset_d=0.62, back=True),
        ),
        default_w=450.0, default_d=450.0, default_h=850.0,
        needs_asset=True,
        height_means="seat",
        keywords=("chair",),
        kinds=("chair",),
    ),
    "stool": FixtureType(
        key="stool",
        bands=(
            Band("legs", 0.00, 0.88, MAT_BODY, inset_w=0.45, inset_d=0.45),
            Band("seat", 0.88, 1.00, MAT_TOP),
        ),
        default_w=350.0, default_d=350.0, default_h=750.0,
        needs_asset=True,
        height_means="seat",
        keywords=("bar chair", "bar stool", "stool"),
        kinds=("stool",),
    ),
    "generic_box": FixtureType(
        key="generic_box",
        bands=(Band("body", 0.0, 1.0, MAT_GENERIC),),
        default_w=800.0, default_d=600.0, default_h=900.0,
        kinds=("generic", "fixture"),
    ),
}

FALLBACK_TYPE = "generic_box"

# Match order matters: longer, more specific keywords must be tried before
# shorter ones that could also match them.
_MATCH_ORDER: tuple[str, ...] = (
    # Seating first: "BAR CHAIR - TRAINING TABLE" names the table it belongs to,
    # so a table keyword would otherwise win.
    "stool", "chair",
    "screen_wall", "light_box", "boxing", "wall_bay", "base_cabinet", "display_panel",
    "service_desk", "island_table", "counter", "shelf_unit", "signage_panel",
    "pillar", "partition",
)

_CODE_INDEX: dict[str, str] = {}
for _key in _MATCH_ORDER:
    _codes = FIXTURE_TYPES[_key].item_codes
    # A bare string would iterate as characters and quietly index nonsense.
    assert isinstance(_codes, tuple), f"{_key}: item_codes must be a tuple"
    for _code in _codes:
        _token = _code.strip().upper()
        # Bare numbers collide with row indices and other numbering schemes;
        # only structured codes ("13-A") are unambiguous enough to key on.
        assert "-" in _token, f"{_key}: ambiguous item code {_code!r}"
        assert _token not in _CODE_INDEX, f"duplicate item code {_token!r}"
        _CODE_INDEX[_token] = _key


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _by_item_code(label: str, kind: str) -> str | None:
    """R-FXT-01 — exact item code. The BOQ path puts the code in both fields."""
    for candidate in (label, kind):
        token = (candidate or "").strip().upper()
        if token and token in _CODE_INDEX:
            return _CODE_INDEX[token]
    return None


def _by_keyword(text: str) -> str | None:
    """R-FXT-02 — description keyword, table order, first match wins."""
    haystack = (text or "").strip().lower()
    if not haystack:
        return None
    for key in _MATCH_ORDER:
        for kw in FIXTURE_TYPES[key].keywords:
            if kw in haystack:
                return key
    return None


def _by_kind(kind: str) -> str | None:
    """R-FXT-03 — the category vocabulary the plan paths emit."""
    token = (kind or "").strip().lower()
    if not token:
        return None
    for key in _MATCH_ORDER:
        if token in FIXTURE_TYPES[key].kinds:
            return key
    if token in FIXTURE_TYPES[FALLBACK_TYPE].kinds:
        return None  # "generic"/"fixture" carry no information
    return None


def _by_dimensions(w: float | None, d: float | None, h: float | None) -> str | None:
    """
    R-FXT-04 — shape heuristic, used when nothing identifies the fixture.

    Pure geometry: a tall thin thing against a wall is a bay, a waist-high deep
    thing is an island. Weak, but strictly better than a featureless box, and it
    is recorded as ``derived`` so its confidence is visible.
    """
    if not (w and d and h) or w <= 0 or d <= 0 or h <= 0:
        return None
    if d < 200 and h >= 1800:
        return "wall_bay"
    if d < 200 and 400 <= h < 1800:
        return "display_panel"
    if h >= 1800 and d < 500:
        return "shelf_unit"
    if 700 <= h <= 1250 and d >= 500:
        return "island_table" if w <= 2000 else "counter"
    if h < 400 and d < 200:
        return "signage_panel"
    return None


def resolve_fixture_type(
    label: str | None,
    kind: str | None,
    width_mm: float | None = None,
    depth_mm: float | None = None,
    height_mm: float | None = None,
) -> tuple[str, str, str]:
    """
    Identify a fixture's type.

    Returns ``(type_key, source, rule_id)`` where ``source`` is a
    ``resolution`` ladder source, so the caller records it like any other
    assumption. The cascade never raises and always returns a usable type.
    """
    lab, knd = label or "", kind or ""

    match = _by_item_code(lab, knd)
    if match:
        return match, "parsed", "R-FXT-01"

    match = _by_keyword(lab)
    if match:
        return match, "parsed", "R-FXT-02"

    match = _by_kind(knd)
    if match:
        return match, "parsed", "R-FXT-03"

    # Last identity attempt: the BOQ path puts description text in `kind`, so
    # treat it as prose once the category vocabulary has had its turn.
    match = _by_keyword(knd)
    if match:
        return match, "parsed", "R-FXT-02"

    match = _by_dimensions(width_mm, depth_mm, height_mm)
    if match:
        return match, "derived", "R-FXT-04"

    return FALLBACK_TYPE, "type_default", "R-FXT-05"


def get_type(key: str) -> FixtureType:
    return FIXTURE_TYPES.get(key, FIXTURE_TYPES[FALLBACK_TYPE])
