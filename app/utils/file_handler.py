import shutil
import uuid
from pathlib import Path

from app.core.config import get_settings


def ensure_dirs() -> None:
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)


def save_upload(project_id: str, filename: str, data: bytes) -> Path:
    ensure_dirs()
    settings = get_settings()
    safe_name = Path(filename).name
    unique = f"{uuid.uuid4().hex[:12]}_{safe_name}"
    dest_dir = settings.upload_dir / str(project_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / unique
    dest.write_bytes(data)
    return dest


def write_output_svg(project_id: str, svg_content: str) -> Path:
    ensure_dirs()
    settings = get_settings()
    out_dir = settings.output_dir / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"layout_{uuid.uuid4().hex[:10]}.svg"
    path = out_dir / name
    path.write_text(svg_content, encoding="utf-8")
    return path


def write_output_dxf(project_id: str, dxf_content: bytes) -> Path:
    ensure_dirs()
    settings = get_settings()
    out_dir = settings.output_dir / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"layout_{uuid.uuid4().hex[:10]}.dxf"
    path = out_dir / name
    path.write_bytes(dxf_content)
    return path


def copy_to_path(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
