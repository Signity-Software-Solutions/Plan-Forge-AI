from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load `.env` from the repo root so OLLAMA_* apply even when uvicorn cwd differs.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _REPO_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE if _ENV_FILE.is_file() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PlanForge AI"
    debug: bool = False
    log_level: str = "INFO"

    upload_dir: Path = Path("uploads")
    output_dir: Path = Path("outputs")

    # Optional S3-style storage (MVP: local only if unset). Secrets: env only, never commit .env
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    aws_access_key_id: str | None = Field(default=None, repr=False)
    aws_secret_access_key: str | None = Field(default=None, repr=False)

    ollama_host: str = ""
    ollama_model: str = ""

    llm_api_url: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_model: str = ""
    llm_timeout_seconds: float = 60.0

    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str = "gpt-4o"

    gemini_api_key: str | None = Field(default=None, repr=False)
    gemini_model: str = "gemini-1.5-flash-001"

    public_api_url: str | None = None

    tesseract_cmd: str | None = None

    planforge_reference_plan_image: str | None = None
    
    planforge_sheet_normalize_mode: str = Field(
        default="heuristic_only",
        description="heuristic_only | llm_merge | llm_labels_only",
    )

    # --- 3D massing (Phase 1 / Tier A) -------------------------------------
    # UnifiedLayout carries no Z data: walls are bare centrelines and there is
    # no ceiling height. These defaults make extrusion possible; every layout
    # that falls back to them is stamped meta.provenance.z_source =
    # "assumed_default" so the viewer can badge it as not-for-construction.
    # Per-layout overrides live in layout.meta (ceiling_height_mm,
    # wall_thickness_mm) and take precedence over these.
    mesh_ceiling_height_mm: float = 3000.0
    mesh_wall_thickness_mm: float = 100.0

    # --- Photoreal render (Stage 2: Blender/Cycles, GPL, subprocess-only) --
    # Never imported into this process -- app/services/photoreal.py only ever
    # shells out to this binary. Unset by default so the app runs fine on a
    # machine without Blender installed; scripts/setup_blender.sh fetches a
    # portable copy to the default path below without needing root.
    blender_bin: str | None = None
    photoreal_samples: int = 128
    photoreal_width: int = 1280
    photoreal_height: int = 720
    # Measured on an 8-thread CPU-only laptop (no discrete GPU): ~65s for one
    # 1280x720/128-sample still of a simple room. Real stores will run longer;
    # this is a circuit breaker against a hung render, not a target time.
    photoreal_timeout_seconds: float = 900.0

    # --- Photoreal turntable (a canned rotation, not a live orbit) ---------
    # There is no hardware path to an orbit-able photoreal view on a CPU-only
    # machine (see app/services/photoreal.py's turntable docstring), so this
    # pre-renders a fixed lap around the room and plays it back as a loop.
    # Lower samples/resolution than a single still, deliberately: measured
    # ~13-17s/frame at these settings on the same 8-thread CPU, so 24 frames
    # is a ~5-7 minute job, not a 30-minute one.
    photoreal_turntable_frames: int = 24
    photoreal_turntable_samples: int = 48
    photoreal_turntable_width: int = 960
    photoreal_turntable_height: int = 540
    # Per-frame timeout (not the whole job's) -- one hung frame must not hang
    # the other 23.
    photoreal_turntable_frame_timeout_seconds: float = 300.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
