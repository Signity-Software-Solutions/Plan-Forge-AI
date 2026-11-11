import logging

from fastapi import APIRouter, HTTPException

from app import state
from app.services.layout_engine import normalize_stored_file, parse_file_on_disk

logger = logging.getLogger(__name__)

router = APIRouter(tags=["process"])


@router.post("/process/{file_id}")
def process_file(file_id: str):
    sf = state.files.get(file_id)
    if sf is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        parsed = parse_file_on_disk(sf)
    except Exception as e:
        logger.exception("Parse failed for file_id=%s", file_id)
        raise HTTPException(status_code=422, detail=str(e)) from e

    sf.parsed_json = parsed

    return {"file_id": sf.id, "project_id": sf.project_id, "parsed": parsed}


@router.post("/normalize/{file_id}")
def normalize_file(file_id: str):
    sf = state.files.get(file_id)
    if sf is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        layout = normalize_stored_file(sf, llm_override=state.runtime_llm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Normalize failed for file_id=%s", file_id)
        raise HTTPException(status_code=422, detail=str(e)) from e

    sf.normalized_json = layout.model_dump(mode="json")

    return {"file_id": sf.id, "project_id": sf.project_id, "normalized": sf.normalized_json}
