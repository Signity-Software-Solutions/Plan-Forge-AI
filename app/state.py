"""In-memory project/file/layout registry for the lifetime of the server process."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class StoredFile:
    id: str
    project_id: str
    type: str
    path: str
    original_filename: str | None = None
    parsed_json: dict[str, Any] | None = None
    normalized_json: dict[str, Any] | None = None


@dataclass
class LayoutRow:
    id: str
    project_id: str
    json_data: dict[str, Any]
    svg_path: str | None = None


projects: dict[str, dict[str, str]] = {}
files: dict[str, StoredFile] = {}
project_files: dict[str, list[str]] = {}
layouts_by_project: dict[str, list[LayoutRow]] = {}

# Runtime LLM config — overrides .env when provider != "env". Resets on server restart.
runtime_llm: dict = {
    "provider": "env",       # env | none | ollama | openai | gemini
    "ollama_host": "",
    "ollama_model": "",
    "api_key": "",
    "model": "",
    "normalize_mode": "llm_labels_only",  # llm_labels_only | llm_merge
}


def register_project(name: str) -> str:
    pid = new_id()
    projects[pid] = {"name": name}
    project_files[pid] = []
    return pid


def register_file(sf: StoredFile) -> None:
    files[sf.id] = sf
    project_files.setdefault(sf.project_id, []).append(sf.id)


def latest_file_for_layout(project_id: str) -> StoredFile | None:
    ids = project_files.get(project_id, [])
    for fid in reversed(ids):
        f = files[fid]
        if f.normalized_json is not None:
            return f
    for fid in reversed(ids):
        f = files[fid]
        if f.parsed_json is not None:
            return f
    return None


def add_layout(row: LayoutRow) -> None:
    layouts_by_project.setdefault(row.project_id, []).append(row)


def latest_layout(project_id: str) -> LayoutRow | None:
    lst = layouts_by_project.get(project_id)
    if not lst:
        return None
    return lst[-1]
