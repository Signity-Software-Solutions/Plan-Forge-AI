# 3D Generation Readiness Audit — PlanForge AI

**Date:** 2026-09-03 · **Branch:** `feature/bge` · **Scope:** can the existing 2D pipeline feed a 3D pipeline?
**Nothing was implemented, installed, or modified. This is an audit only.**

---

## 1. Executive Summary

**Answer: YES, WITH SOME CHANGES.** The foundation is good. The 3D layer is simply absent.

The system already produces clean, real-world **millimetre** geometry with a single origin — walls, rooms, openings and fixtures with width/depth/height/rotation. That is exactly the kind of data a 3D builder needs. It is *parametric* data, not a picture, so **no AI 3D model is required**.

What is missing is the **third dimension**: nothing in the system stores how tall a wall is, how high the ceiling is, or how far off the floor a display is mounted. Also missing: a fixed list of fixture types, and wall thickness (which is read from the input file but then thrown away).

**Overall 3D Readiness: 48 / 100.** Strong 2D base, zero 3D layer.

---

## 2. Existing Architecture

Two stacks live in the repo. **Both converge on the same data contract**, which is why 3D is cheap to add.

| Stack | Entry point | Storage | Status |
|---|---|---|---|
| POC / legacy | [app/main.py](app/main.py) + [app/api/](app/api/) | in-memory + `outputs/` on disk | working, used by Studio UI |
| Enterprise v1 | [app/presentation/app.py](app/presentation/app.py) + [app/presentation/api/v1.py](app/presentation/api/v1.py) | Postgres + pgvector, Celery, S3/local | working, Clean Architecture |

The single shared contract is **`UnifiedLayout`** — [app/models/layout_schema.py](app/models/layout_schema.py) (51 lines), re-exported to the domain at [app/domain/value_objects/layout.py](app/domain/value_objects/layout.py).

Renderers are already behind ports (`IRenderer`, `IExporter` in [app/domain/interfaces/ports.py](app/domain/interfaces/ports.py)), so **a third output format plugs in without touching the pipeline.**

---

## 3. Existing 2D Pipeline (traced from code, not README)

```
Upload (cad | image | sheet)
   │
   ├── DXF   → app/services/cad_parser.py     (ezdxf: LINE/LWPOLYLINE → walls, TEXT → labels)
   ├── Image → app/services/image_parser.py   (OpenCV Hough lines + Tesseract OCR)
   └── Sheet → app/services/sheet_parser.py   (xlsx / csv / zip / pdf — 1763 lines, richest path)
   │
   ▼
Normalization — app/core/llm_client.py
   • heuristic_normalize()  ← default, and the ONLY path for sheets
   • optional LLM (Ollama / OpenAI / Gemini), gated by app/services/layout_llm_guard.py
   │
   ▼
UnifiedLayout  (units, walls[], rooms[], fixtures[], openings[], meta{})
   │
   ▼
light_snap_walls()  — app/services/geometry_engine.py:997
   │
   ├──► layout_to_svg()          → SVG   (geometry_engine.py:1033)
   └──► export_layout_to_dxf()   → DXF   (dxf_exporter.py:981)  ← 2D only
```

The assumed flow in the brief is correct. Confirmed.

---

## 4. Existing Data Model

`UnifiedLayout` — the complete, actual definition:

| Entity | Fields present today |
|---|---|
| **WallSegment** | `x1, y1, x2, y2` — **that is all** |
| **Room** | `name, area, polygon[[x,y]…]` |
| **Opening** | `kind, x1, y1, x2, y2, label` |
| **Fixture** | `kind, x, y, label, width_mm, depth_mm, height_mm, rotation_deg` |
| **meta** | free-form dict (project info, dimension strings, vendor tables) |

**Geometric fields:** all coordinates, `polygon`, `width_mm`, `depth_mm`, `height_mm`, `rotation_deg`, `area`.
**Semantic fields:** `kind`, `label`, `name` — all free text, no controlled list.

### Data that exists in the input file but is LOST by normalization

| Field | Read at | Dropped at |
|---|---|---|
| `thickness_mm` (wall) | [sheet_parser.py:1550](app/services/sheet_parser.py#L1550); column exists in `samples/planforge_sample.csv` | not read by `_read_explicit_walls_df` ([sheet_parser.py:193](app/services/sheet_parser.py#L193)); not in schema |
| `zone` (e.g. `REAR_WALL`) | [sheet_parser.py:1623](app/services/sheet_parser.py#L1623) | never emitted ([sheet_parser.py:870](app/services/sheet_parser.py#L870)) |
| `model_ref` / `model_number` | [sheet_parser.py:1612](app/services/sheet_parser.py#L1612) | not in schema |
| `layer` (wall) | [sheet_parser.py:1552](app/services/sheet_parser.py#L1552) | not in schema |
| `existing_rcc_slab_ht_mm` = 4450 | survives in `meta.projectmeta` as a **string** | never used by any code |

This is good news: **wall thickness and ceiling height are already in the customer's spreadsheets.** They just are not carried through.

---

## 5. 2D Output Analysis

What the system knows today:

| Question | Answer |
|---|---|
| Wall coordinates | ✅ Yes — absolute mm, single origin |
| Wall thickness | ⚠️ In the input file; **not in the model**. SVG line width is cosmetic only ([geometry_engine.py:1078](app/services/geometry_engine.py#L1078)) |
| Wall height | ❌ **Not found in the current project** |
| Room boundaries / dimensions | ✅ Yes — closed polygons (rectangles only) |
| Doors / windows / openings | ⚠️ Position + type only. No height, no sill, no swing, no link to a wall |
| Floor boundary | ✅ Derivable from room polygons |
| Ceiling | ❌ Only a text field `existing_rcc_slab_ht_mm` in one sample |
| Fixture position | ✅ Yes — absolute mm centre |
| Fixture dimensions | ✅ Yes — `width_mm`, `depth_mm`, `height_mm` |
| Fixture orientation | ✅ Yes — `rotation_deg` (Z-axis) |
| Fixture elevation / mounting height | ❌ **Missing** |
| Object relationships | ❌ No object→room, no opening→wall, no mount type |
| Absolute coordinates | ✅ Yes, consistent |
| Materials / textures | ❌ **Nothing anywhere in the codebase** |

**Semantic identity is weak.** `kind` is uncontrolled free text and differs by input path:
- curated sheets → `display`, `table`, `shelving`, `counter`, `chair`
- BOQ Excel → item codes such as `12-K`, `13-A`, `30-F` (measured across `outputs/*/layout.json`)

Opening kinds observed: `door`, `entry`, `opening`, `storefront`, `fire_door`, `window` — usable, but not enforced.

**Not found in the current project:** LFD/kiosk/brand-wall/signage/HVAC/speaker/camera/lighting/electrical/network as *typed* entities. They exist only as free-text labels inside `fixtures`.

---

## 6. 3D Requirements (gap matrix)

| Entity | Current 2D data | 3D requirement | Available? | Gap |
|---|---|---|---|---|
| Wall | x1,y1,x2,y2 | length, thickness, height, base level, material | **Partial** | thickness (in input, dropped), height, material |
| Door | x1,y1,x2,y2, kind | width, height, sill=0, host wall, swing | **Partial** | height, host wall link, swing |
| Window | x1,y1,x2,y2, kind | width, height, sill height, host wall | **Partial** | height, sill, host wall link |
| Fixture | x,y,W,D,H,rotation | XYZ, dimensions, orientation, category, asset | **Partial** | Z / mounting height, controlled category, 3D asset |
| Room | polygon, name, area | floor + ceiling height → volume | **Partial** | ceiling height, floor level |
| Materials | — | flooring, wall finish, glass, metal, paint | **No** | entire concept missing |
| Relationships | — | in-room, on-wall, on-floor, on-ceiling | **No** | `zone` is parsed then discarded |

---

## 7. Data Sufficiency Analysis

### ✅ AVAILABLE (already in `UnifiedLayout`)
Wall centrelines · room polygons · opening positions and types · fixture centres · fixture W/D/H · fixture rotation · units (mm) · consistent origin.

### 🟡 DERIVABLE (calculable from what exists — no new input needed)
- Floor slabs → from room polygons
- Wall thickness → **already in the source spreadsheet**, just needs to be carried through
- Ceiling height → from `meta.projectmeta.existing_rcc_slab_ht_mm` where present
- Which room an object is in → point-in-polygon (Shapely is already a dependency)
- Which wall an opening belongs to → nearest-collinear-segment match
- Wall-mounted vs floor-standing → from `zone` (`REAR_WALL`) once it is no longer dropped

### 🔴 REQUIRED BUT MISSING (must be added)
- **Wall height** — no source at all
- **Fixture mounting height (Z)** — no source at all
- **Opening height and window sill height** — no source at all
- **Controlled fixture category list** — needed to pick a 3D shape deterministically
- A **3D output format, endpoint and viewer** — none exist

### ⚪ OPTIONAL (visual polish only)
Real materials and textures · PBR shading · manufacturer 3D models · photorealistic rendering · walkthrough / 360°.

---

## 8. Hardware Analysis

Three different things are being confused in general discussion. They are separated here.

| Workload | Needs GPU? | Assessment |
|---|---|---|
| **3D scene construction** (extrude walls, build floors, write GLB) | **No** | Pure CPU maths. Milliseconds to a few seconds. Fits the existing **4 vCPU / 8 GB** app node ([docs/PlanForge_Infrastructure_and_ThirdParty_Costs.md](docs/PlanForge_Infrastructure_and_ThirdParty_Costs.md) §1) with room to spare |
| **3D viewing** (browser preview) | **No server GPU** | Rendering happens on the *user's* laptop via WebGL |
| **Photorealistic offline rendering** | Yes — but **not in scope** | Only if the client later asks for marketing-quality images |
| **AI 3D asset generation** | Yes — **not recommended, not needed** | Would destroy dimensional accuracy |

The **A100 80 GB** referenced in the cost document is for the **LLM tier**, not for 3D. It is unrelated to this phase.

**Extra storage:** small. A GLB for a store of this size is roughly 0.5–5 MB — the same order as the DXF files already stored.

---

## 9. Third-Party Integration Analysis

| Service | Current purpose | Reuse for 3D? | New service needed? |
|---|---|---|---|
| Ollama / OpenAI / Gemini / Azure / OpenRouter | LLM label + layout normalisation | Not for geometry | No |
| Tesseract OCR | image text | No | No |
| PostgreSQL + pgvector | documents, outputs, embeddings | ✅ Yes — add a `glb_key` column | No |
| Redis + Celery | background jobs | ✅ Yes — run 3D builds async | No |
| S3 / local storage ([app/infrastructure/storage/](app/infrastructure/storage/)) | SVG + DXF artefacts | ✅ Yes — store GLB the same way | No |
| BGE-M3 (local, CPU) | embeddings | No | No |

**New third-party services required: NONE.** Two open-source Python libraries are enough (`trimesh` + `pygltflib`, or `trimesh` alone), plus `three.js` from a CDN in the browser. `shapely` is already installed and covers the polygon work.

Confirmed: **no 3D library is currently installed** (checked `requirements.txt` and `.venv`).

---

## 10. 3D Architecture Options

| Approach | Verdict |
|---|---|
| **A. Procedural extrusion from existing geometry** | ✅ **RECOMMENDED** — the data is already parametric mm geometry; deterministic, accurate, CPU-only, free |
| B. Send geometry to an external 3D engine/API | ❌ Unnecessary cost and a data-boundary risk for no benefit |
| C. Convert to BIM/IFC first, then render | ❌ Later. IFC needs building semantics (storeys, spaces, systems) the model does not have |
| D. AI-generated 3D model | ❌ **Not required.** It produces *plausible* geometry, not *dimensionally correct* geometry. Wrong tool for a construction drawing |

Keep the four concepts separate: **deterministic 3D construction** (this phase) ≠ **visualisation** ≠ **AI-generated assets** ≠ **photorealistic rendering**.

---

## 11. Recommended Architecture (smallest possible extension)

```
Existing Input  →  Existing Parser  →  Existing Normalization
                            ↓
                 UnifiedLayout  (extended with optional Z fields)
                            ↓
          ┌─────────────────┼──────────────────────┐
          ↓                 ↓                      ↓
   layout_to_svg     export_layout_to_dxf     NEW scene3d_builder   ← new
     (unchanged)         (unchanged)                ↓
        SVG                  DXF               NEW gltf_exporter    ← new
                                                    ↓
                                                  .glb
                                                    ↓
                                       NEW three.js viewer in Studio  ← new
```

**Recommended output format: glTF 2.0 / GLB.** One self-contained file, opens natively in every browser, small, open standard, writable from pure Python on CPU. IFC, USD and FBX are heavier and solve problems this phase does not have. (Optional later: an IFC export for the client's BIM team.)

---

## 12. Reuse / Extend / New Matrix

### REUSE — unchanged
- All three parsers: [cad_parser.py](app/services/cad_parser.py), [image_parser.py](app/services/image_parser.py), [sheet_parser.py](app/services/sheet_parser.py)
- SVG renderer [geometry_engine.py](app/services/geometry_engine.py) and DXF exporter [dxf_exporter.py](app/services/dxf_exporter.py)
- Studio UI shell, upload/process/normalize routes, LLM guard, Celery, storage, Postgres

### EXTEND — small, additive, backward-compatible changes
| File | Change |
|---|---|
| [app/models/layout_schema.py](app/models/layout_schema.py) | add **optional** 3D fields (all default to `None` → old data still loads) |
| [app/core/llm_client.py](app/core/llm_client.py) `heuristic_normalize` | stop discarding `thickness_mm`, `zone`, `model_ref` |
| [app/services/sheet_parser.py:193](app/services/sheet_parser.py#L193) `_read_explicit_walls_df` | read `thickness_mm` |
| [app/utils/validators.py](app/utils/validators.py) | add 3D sanity checks (height > 0, opening fits inside its wall) |
| [app/domain/interfaces/ports.py](app/domain/interfaces/ports.py) | add `IScene3DExporter` port |
| [app/presentation/routers/pipeline.py](app/presentation/routers/pipeline.py) | add `POST /normalized/{id}/render3d` and `GET /outputs/{id}/glb` |
| [app/infrastructure/database/models/ai.py](app/infrastructure/database/models/ai.py) | add `glb_key` to `GeneratedOutput` (+ one Alembic migration) |
| [app/templates/studio.html](app/templates/studio.html) | add a "3D" tab |

### NEW components
- `app/services/scene3d_builder.py` — UnifiedLayout → 3D scene graph
- `app/services/gltf_exporter.py` — scene graph → `.glb`
- `app/services/fixture_catalog.py` — fixture category → 3D shape/proxy rules
- `app/static/viewer3d.js` — three.js GLB viewer

---

## 13. BLOCKERS

> These make accurate 3D **impossible**, not merely ugly.

1. **No Z / height data anywhere.** No wall height, no ceiling height, no fixture mounting height. Without this there is literally no third dimension to build.
2. **Wall thickness is dropped.** A wall is a zero-width line in the model, so it cannot be turned into a solid. *(Easy fix — the number is already in the customer's spreadsheet.)*
3. **Openings are not linked to walls and have no height.** Doors and windows cannot be cut into walls; today they are only drawn as dashed lines *on top of* walls ([geometry_engine.py:762](app/services/geometry_engine.py#L762)).
4. **No controlled fixture category list.** `kind` is free text and inconsistent between input paths (`display` vs `12-K`), so no rule can reliably decide what shape to build.

---

## 14. IMPORTANT GAPS

- No material or finish field on any entity.
- `zone` (`REAR_WALL`) is parsed then thrown away — this is the natural source for "wall-mounted vs floor-standing".
- Room polygons are rectangles only; L-shaped and angled rooms are not supported.
- `height_mm` semantics are ambiguous in the sample data (a 65″ display is recorded with `height_mm = 150`). **Confirm with the client whether this column means object height or something else** before trusting it in 3D.
- No storage slot, endpoint, or viewer for a 3D file yet.

---

## 15. OPTIONAL IMPROVEMENTS

Textures and PBR materials · real manufacturer 3D models (GLB/OBJ) for hero fixtures · photorealistic rendering · first-person walkthrough · 360° panoramas · IFC/BIM export · lighting simulation.

---

## 16. 3D Readiness Score

| Category | Weight | Score | Note |
|---|---:|---:|---|
| 2D geometry | 15 | **12** | Clean mm vectors; walls are centrelines only |
| Spatial coordinates | 15 | **14** | Absolute mm, single consistent origin |
| Semantic model | 10 | **4** | `kind` is uncontrolled free text |
| Object dimensions | 10 | **7** | Fixtures have W/D/H; walls have none |
| Elevation / Z data | 10 | **1** | Effectively absent |
| Materials | 10 | **0** | Concept does not exist in the codebase |
| 3D assets | 10 | **0** | None |
| Scene relationships | 10 | **2** | Derivable, but not modelled |
| Rendering infrastructure | 5 | **3** | Renderer ports + storage ready for a third format |
| Hardware / infrastructure | 5 | **5** | Existing CPU node is sufficient |
| **TOTAL** | **100** | **48** | |

**Why 48:** the *spatial* half of the problem is nearly solved (26/30 on geometry and coordinates); the *3D-specific* half — height, materials, assets, relationships — scores 7 out of 40. The foundation is real; the 3D layer has not been started.

---

## 17. MVP 3D Scope

**Produces:** a dimensionally correct, flat-shaded "massing" model of the store, viewable in the browser.

```
Normalized layout  →  3D scene builder  →  walls + floor + openings
                                        →  fixtures as sized boxes
                                        →  flat colours by category
                                        →  GLB  →  three.js viewer in Studio
```

**MVP supports**
- Walls extruded to a real thickness and a project-level default height
- Floor slabs from room polygons; optional flat ceiling
- Door/window/storefront openings cut through walls
- Every fixture as a correctly sized, correctly rotated, correctly placed box
- Wall-mounted fixtures lifted to a sensible default height by category
- Flat colour per category, orbit/pan/zoom, GLB download

**MVP intentionally does NOT support**
- Photorealism, textures, or real lighting
- Manufacturer-accurate 3D models
- Curved or sloped walls, multiple floors, stairs
- Ceilings with services, MEP, HVAC
- Walkthrough / VR / 360°
- IFC or BIM export
- Any AI-generated geometry

---

## 18. Phase-wise Implementation Plan

| Phase | Work | Rough size |
|---|---|---|
| **3.0 — Data** | Add optional 3D fields to `UnifiedLayout`; stop dropping `thickness_mm` / `zone`; add a project-level default wall & ceiling height (seeded from `existing_rcc_slab_ht_mm`); agree a fixture category list with the client | Small |
| **3.1 — Builder** | `scene3d_builder.py`: extrude walls, build floors, cut openings, place fixture boxes; derive room membership and wall/floor mounting | Medium |
| **3.2 — Export** | `gltf_exporter.py` → GLB; add `IScene3DExporter` port, `glb_key` column + migration, `/render3d` and `/glb` endpoints; store via existing S3/local storage | Small |
| **3.3 — Viewer** | three.js tab in Studio: orbit, click-to-inspect a fixture, GLB download | Small–Medium |
| **3.4 — Quality (later)** | Materials, real 3D assets for hero fixtures, IFC export, rendering | Optional |

---

## 19. Final YES/NO Assessment

| Question | Answer |
|---|---|
| 1. Is the current architecture sufficient to start 3D? | **YES WITH MODIFICATIONS** |
| 2. Can the current 2D output be the source for 3D? | **PARTIALLY** — geometry yes, height/materials no |
| 3. Single biggest missing capability? | **Height / Z data.** No wall height, ceiling height or mounting height exists anywhere |
| 4. Minimum change required? | Add optional Z fields to `UnifiedLayout`, stop discarding wall thickness and `zone`, agree a fixture category list |
| 5. Do we need new hardware? | **NO** — the existing CPU node is enough. The A100 in the cost doc is for the LLM, not 3D |
| 6. New third-party software/services? | **NO paid services.** Two open-source Python libraries plus three.js in the browser |
| 7. New 3D asset data? | **PARTIALLY** — not for the MVP (boxes are enough). Yes later, for realistic hero fixtures |
| 8. Can the existing schema be extended rather than replaced? | **YES** — all new fields are optional, so old projects keep working |
| 9. What should the next phase build? | Scene builder → GLB exporter → browser viewer (see §17) |
| 10. What should we NOT build yet? | AI 3D generation, photorealistic rendering, IFC/BIM, walkthrough/VR, GPU rendering infrastructure |

---

## FINAL DECISION

**We can move to 3D with the current system — but three things must be added first.**

The hard part is already done. The system produces accurate, real-world millimetre geometry with room boundaries, openings and correctly sized, correctly rotated fixtures. That is a genuine 3D-ready foundation, and it means we can build the 3D model by **calculation, not by AI** — which is faster, cheaper, and dimensionally correct.

Before starting the 3D phase we need:

1. **Height information** — wall height, ceiling height, and how high wall-mounted items sit. This is the blocker; today it does not exist. *(A single project-level default plus a per-category default is enough to start.)*
2. **Wall thickness carried through** — the number is already in the customer's spreadsheet, our code just discards it.
3. **A fixed list of fixture types** — agreed with the client, so each item maps to a known 3D shape.

We do **not** need new hardware, a GPU, a paid 3D service, or an AI 3D-generation model.

**Recommended first version:** walls, floors, door/window openings, and fixtures as accurately sized blocks, exported as a `.glb` file and viewed in the browser inside Studio. Realistic materials and manufacturer models come later.
