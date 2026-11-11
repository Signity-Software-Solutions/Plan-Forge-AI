import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response

from app import state
from app.services.layout_engine import generate_layout_record
from app.utils.output_disk import latest_dxf_path, latest_svg_path, load_layout_json_from_disk

logger = logging.getLogger(__name__)

router = APIRouter(tags=["output"])

_NORM_META_KEYS = (
    "normalized_by",
    "llm_candidate_rejected",
    "rejection_reason",
    "llm_gates_passed",
    "llm_labels_only",
    "llm_rejection_source",
)


def _normalization_summary(meta: dict | None) -> dict[str, object]:
    """Subset of layout.meta for clients (normalization / LLM gate audit)."""
    if not meta:
        return {}
    return {k: meta[k] for k in _NORM_META_KEYS if k in meta and meta[k] is not None}


@router.post("/generate-layout/{project_id}")
def generate_layout(project_id: str):
    if project_id not in state.projects:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        row, layout, issues = generate_layout_record(project_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("generate-layout failed project_id=%s", project_id)
        raise HTTPException(status_code=422, detail=str(e)) from e

    return {
        "layout_id": row.id,
        "project_id": project_id,
        "svg_path": row.svg_path,
        "json": layout.model_dump(mode="json"),
        "normalization": _normalization_summary(layout.meta),
        "validation_issues": [
            {"code": i.code, "message": i.message, "detail": i.detail} for i in issues
        ],
    }


@router.get("/output/{project_id}")
def get_output(project_id: str):
    row = state.latest_layout(project_id)
    if row is not None:
        svg_text = None
        if row.svg_path and Path(row.svg_path).exists():
            svg_text = Path(row.svg_path).read_text(encoding="utf-8")
        jd = row.json_data or {}
        meta = jd.get("meta") if isinstance(jd, dict) else {}
        return {
            "project_id": project_id,
            "layout_id": row.id,
            "json": row.json_data,
            "svg": svg_text,
            "svg_path": row.svg_path,
            "normalization": _normalization_summary(meta if isinstance(meta, dict) else {}),
        }

    disk_json = load_layout_json_from_disk(project_id)
    disk_svg = latest_svg_path(project_id)
    if disk_json is None and disk_svg is None:
        raise HTTPException(status_code=404, detail="No layout output for project")

    svg_text = None
    svg_path_str = None
    if disk_svg is not None and disk_svg.exists():
        svg_text = disk_svg.read_text(encoding="utf-8")
        svg_path_str = str(disk_svg)

    meta_d: dict = {}
    if isinstance(disk_json, dict):
        m = disk_json.get("meta")
        if isinstance(m, dict):
            meta_d = m
    return {
        "project_id": project_id,
        "layout_id": None,
        "json": disk_json,
        "svg": svg_text,
        "svg_path": svg_path_str,
        "normalization": _normalization_summary(meta_d),
        "restored_from_disk": True,
    }


@router.get("/output/{project_id}/svg", response_class=PlainTextResponse)
def get_output_svg(project_id: str):
    row = state.latest_layout(project_id)
    if row is not None and row.svg_path:
        path = Path(row.svg_path)
        if path.exists():
            return path.read_text(encoding="utf-8")
    disk = latest_svg_path(project_id)
    if disk is not None and disk.exists():
        return disk.read_text(encoding="utf-8")
    raise HTTPException(status_code=404, detail="No SVG for project")


@router.get("/output/{project_id}/dxf")
def get_output_dxf(project_id: str):
    """
    Generate (or return cached) a civil-grade DXF for the project.

    The DXF is built on-demand from the latest layout JSON.
    It includes:
      - Walls, rooms (with hatch), fixtures, openings on separate layers
      - Per-fixture linear dimensions (width + depth)
      - Overall store dimensions (all four sides)
      - Partition-to-partition dimension chains (X and Y)
      - Elevation / section callout markers (A–D)
      - Fixture schedule / BOM table
      - Title block (project, client, scale, drawing number, revision)
      - Graphic scale bar + north arrow
    """
    # Resolve layout from memory or disk
    row = state.latest_layout(project_id)
    layout_data: dict | None = None

    if row is not None and row.json_data:
        layout_data = row.json_data
    else:
        layout_data = load_layout_json_from_disk(project_id)

    if layout_data is None:
        raise HTTPException(
            status_code=404,
            detail="No layout found for project. Run POST /generate-layout/{project_id} first.",
        )

    try:
        from app.models.layout_schema import UnifiedLayout
        from app.services.dxf_exporter import export_layout_to_dxf
        from app.utils.file_handler import write_output_dxf
        import tempfile, os

        layout = UnifiedLayout.model_validate(layout_data)

        # Write to a temp file, read bytes, then persist to outputs/
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            export_layout_to_dxf(layout, tmp_path)
            dxf_bytes = tmp_path.read_bytes()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Persist alongside SVG
        out_path = write_output_dxf(project_id, dxf_bytes)
        logger.info("DXF generated project_id=%s path=%s", project_id, out_path)

    except Exception as exc:
        logger.exception("DXF generation failed project_id=%s", project_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    filename = f"planforge_{project_id[:8]}.dxf"
    return Response(
        content=dxf_bytes,
        media_type="application/dxf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/output/{project_id}/glb")
def get_output_glb(project_id: str):
    """
    3D massing model (Tier A) as GLB, built on demand from the latest layout.

    Walls, fixtures and ceiling slabs as plain boxes on separate scene groups
    (``walls`` / ``fixtures`` / ``ceiling``), one flat neutral shade each, with
    dimension callouts and Z provenance in the glTF scene ``extras``.

    **Never written to disk.** Unlike the DXF route above, this response is not
    persisted: generation is ~1s and deterministic, so a cache miss is cheap,
    while storing every generated GLB would grow the output volume without
    bound. Do not add a save-to-disk path here as a convenience.
    """
    row = state.latest_layout(project_id)
    layout_data: dict | None = None

    if row is not None and row.json_data:
        layout_data = row.json_data
    else:
        layout_data = load_layout_json_from_disk(project_id)

    if layout_data is None:
        raise HTTPException(
            status_code=404,
            detail="No layout found for project. Run POST /generate-layout/{project_id} first.",
        )

    try:
        from app.models.layout_schema import UnifiedLayout
        from app.services.mesh_builder import EmptyMassingError, build_massing

        layout = UnifiedLayout.model_validate(layout_data)
        result = build_massing(layout)
        glb_bytes = result.export_glb()
    except EmptyMassingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GLB generation failed project_id=%s", project_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info(
        "GLB generated project_id=%s bytes=%d z_source=%s",
        project_id,
        len(glb_bytes),
        result.provenance.get("z_source"),
    )

    filename = f"planforge_{project_id[:8]}.glb"
    return Response(
        content=glb_bytes,
        media_type="model/gltf-binary",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            # Regenerated per request; the hash lets the browser skip re-download.
            "ETag": f'"{result.vertex_hash()[:32]}"',
            "Cache-Control": "no-store",
            "X-PlanForge-Z-Source": str(result.provenance.get("z_source", "")),
        },
    )


@router.get("/output/{project_id}/massing-meta")
def get_output_massing_meta(project_id: str):
    """
    Dimension callouts, viewpoints, Z provenance and scene stats.

    Same data as the GLB's scene ``extras``; served separately so the viewer can
    render dimension labels as real text without parsing the binary.

    ``resolutions`` is the full audit trail: every value the layout did not
    carry, the rule that supplied it and how much to trust it.
    """
    row = state.latest_layout(project_id)
    layout_data = row.json_data if (row is not None and row.json_data) else None
    if layout_data is None:
        layout_data = load_layout_json_from_disk(project_id)
    if layout_data is None:
        raise HTTPException(status_code=404, detail="No layout found for project")

    try:
        from app.models.layout_schema import UnifiedLayout
        from app.services.mesh_builder import build_massing

        result = build_massing(UnifiedLayout.model_validate(layout_data))
    except Exception as exc:
        logger.exception("massing-meta failed project_id=%s", project_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "project_id": project_id,
        "provenance": result.provenance,
        "dimensions": result.dimensions,
        "viewpoints": result.viewpoints,
        "stats": result.stats,
        # Every value the layout did not carry, with the rule that supplied it.
        "resolutions": result.resolutions,
    }


def _resolve_layout_data(project_id: str) -> dict | None:
    row = state.latest_layout(project_id)
    if row is not None and row.json_data:
        return row.json_data
    return load_layout_json_from_disk(project_id)


@router.post("/output/{project_id}/render")
def start_photoreal_render(project_id: str, viewpoint_id: str | None = None):
    """
    Kick off a Stage 2 photoreal render (Blender/Cycles) for the project's
    current layout. Runs in the background — poll GET .../render/status,
    then fetch GET .../render.png once status is "done".

    ``viewpoint_id`` (optional query param) picks from the same named
    viewpoints the 3D Massing tab's dropdown offers (see .../massing-meta's
    ``viewpoints``); omitted or unrecognised both fall back to "PLAN". Only
    one render (and one PNG) per project — a different viewpoint replaces it
    rather than being cached alongside it.

    Blender is invoked as a subprocess, never imported into this process (it
    is GPL-licensed and ~1GB) — see app/services/photoreal.py. 503 if no
    Blender binary is configured (run scripts/setup_blender.sh).
    """
    layout_data = _resolve_layout_data(project_id)
    if layout_data is None:
        raise HTTPException(
            status_code=404,
            detail="No layout found for project. Run POST /generate-layout/{project_id} first.",
        )

    from app.models.layout_schema import UnifiedLayout
    from app.services import photoreal

    try:
        layout = UnifiedLayout.model_validate(layout_data)
        result = photoreal.start_render(project_id, layout, viewpoint_id)
    except photoreal.BlenderNotAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except photoreal.RenderAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("photoreal render start failed project_id=%s", project_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("photoreal render started project_id=%s", project_id)
    return result


@router.get("/output/{project_id}/render/status")
def get_photoreal_render_status(project_id: str):
    from app.services import photoreal

    return photoreal.get_status(project_id)


@router.get("/output/{project_id}/render.png")
def get_photoreal_render(project_id: str):
    from app.services import photoreal

    path = photoreal.render_output_path(project_id)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No photoreal render yet. POST /output/{project_id}/render first.",
        )
    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/output/{project_id}/turntable")
def start_photoreal_turntable(project_id: str):
    """
    Kick off a photoreal turntable: a fixed sequence of stills spaced evenly
    around a full circle, later stitched into a looping GIF — a pre-rendered
    lap to watch, not a live orbit (see app/services/photoreal.py's module
    comment for why an orbit-able *photoreal* view isn't possible on
    CPU-only hardware). Runs in the background; poll
    GET .../turntable/status, then fetch GET .../turntable.gif once done.

    Takes several minutes (frame count/quality are server-side settings, see
    README's Photoreal render section) — POST .../turntable/cancel to stop
    it early.
    """
    layout_data = _resolve_layout_data(project_id)
    if layout_data is None:
        raise HTTPException(
            status_code=404,
            detail="No layout found for project. Run POST /generate-layout/{project_id} first.",
        )

    from app.models.layout_schema import UnifiedLayout
    from app.services import photoreal

    try:
        layout = UnifiedLayout.model_validate(layout_data)
        result = photoreal.start_turntable(project_id, layout)
    except photoreal.BlenderNotAvailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except photoreal.TurntableAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("turntable render start failed project_id=%s", project_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("turntable render started project_id=%s", project_id)
    return result


@router.get("/output/{project_id}/turntable/status")
def get_photoreal_turntable_status(project_id: str):
    from app.services import photoreal

    return photoreal.get_turntable_status(project_id)


@router.post("/output/{project_id}/turntable/cancel")
def cancel_photoreal_turntable(project_id: str):
    from app.services import photoreal

    return photoreal.cancel_turntable(project_id)


@router.get("/output/{project_id}/turntable.gif")
def get_photoreal_turntable(project_id: str):
    from app.services import photoreal

    path = photoreal.turntable_output_path(project_id)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="No turntable render yet. POST /output/{project_id}/turntable first.",
        )
    return Response(
        content=path.read_bytes(),
        media_type="image/gif",
        headers={"Cache-Control": "no-store"},
    )
