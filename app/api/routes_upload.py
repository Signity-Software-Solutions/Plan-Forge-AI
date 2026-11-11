import logging
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app import state
from app.utils.file_handler import save_upload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])


@router.post("/upload")
@router.post("/upload/")
async def upload_file(
    file: Annotated[UploadFile, File()],
    type: Annotated[Literal["cad", "image", "sheet"], Form()],
    project_id: Annotated[str | None, Form()] = None,
    project_name: Annotated[str | None, Form()] = None,
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    if project_id is None:
        pid = state.register_project(project_name or "Untitled")
    else:
        if project_id not in state.projects:
            raise HTTPException(status_code=404, detail="Project not found")
        pid = project_id

    fid = state.new_id()
    dest = save_upload(pid, file.filename, data)
    sf = state.StoredFile(
        id=fid,
        project_id=pid,
        type=type,
        path=str(dest),
        original_filename=file.filename,
    )
    state.register_file(sf)

    logger.info("Uploaded file_id=%s project_id=%s type=%s path=%s", fid, pid, type, dest)

    return {
        "file_id": fid,
        "project_id": pid,
        "type": type,
        "path": str(dest),
        "original_filename": file.filename,
    }
