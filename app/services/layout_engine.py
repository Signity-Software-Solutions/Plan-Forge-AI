"""Pipeline helpers: parse → normalize payload → validate → SVG."""

import json
import logging
from pathlib import Path

from app import state
from app.core.config import get_settings
from app.core.llm_client import heuristic_normalize, normalize_with_llm
from app.models.layout_schema import UnifiedLayout
from app.services.cad_parser import parse_dxf
from app.services.geometry_engine import layout_to_svg, light_snap_walls
from app.services.image_parser import parse_image
from app.services.sheet_parser import parse_sheet
from app.utils.file_handler import write_output_svg
from app.utils.output_disk import layout_json_path
from app.utils.validators import validate_layout

logger = logging.getLogger(__name__)


def parse_file_on_disk(sf: state.StoredFile) -> dict:
    path = Path(sf.path)
    if sf.type == "cad":
        return parse_dxf(path)
    if sf.type == "image":
        return parse_image(path)
    if sf.type == "sheet":
        return parse_sheet(path)
    raise ValueError(f"Unknown file type: {sf.type}")


def normalize_stored_file(sf: state.StoredFile, llm_override: dict | None = None) -> UnifiedLayout:
    if not sf.parsed_json:
        raise ValueError("File has no parsed_json; run /process first.")
    payload = {
        "parsed": sf.parsed_json,
        "file_type": sf.type,
        "filename": sf.original_filename or "",
    }
    return normalize_with_llm(payload, llm_override=llm_override)


def generate_layout_record(project_id: str) -> tuple[state.LayoutRow, UnifiedLayout, list]:
    """Latest normalized (or parsed) file for project; validate; write SVG; record layout in memory."""
    sf = state.latest_file_for_layout(project_id)
    if sf is None:
        raise ValueError("No processed files for project.")
    if sf.normalized_json is None:
        layout = heuristic_normalize(sf.parsed_json or {})
    else:
        layout = UnifiedLayout.model_validate(sf.normalized_json)

    layout = light_snap_walls(layout)
    issues = validate_layout(layout)

    underlay_path: Path | None = None
    if sf.type == "image":
        p = Path(sf.path)
        if p.is_file():
            underlay_path = p

    settings = get_settings()
    ref_raw = (settings.planforge_reference_plan_image or "").strip()
    ref_path = Path(ref_raw) if ref_raw else None
    reference_plan = ref_path if ref_path and ref_path.is_file() else None

    svg = layout_to_svg(
        layout,
        underlay_png_path=underlay_path,
        reference_plan_path=reference_plan,
    )
    out_path = write_output_svg(project_id, svg)

    row = state.LayoutRow(
        id=state.new_id(),
        project_id=project_id,
        json_data=layout.model_dump(mode="json"),
        svg_path=str(out_path),
    )
    state.add_layout(row)
    try:
        dump = layout_json_path(project_id)
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps(layout.model_dump(mode="json"), indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write layout.json for project_id=%s: %s", project_id, e)
    return row, layout, issues
