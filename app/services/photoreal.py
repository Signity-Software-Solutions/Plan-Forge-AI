"""
Photoreal render orchestration (Stage 2).

This module is pure Python and safe to import anywhere in the app -- it never
imports ``bpy``. Blender is GPL-licensed and is invoked exactly once, as a
subprocess (``app/blender/render_scene.py``), the same boundary
``ARCH-POC/verify.md`` calls for ("Run as a subprocess, never linked").

Job tracking is a plain in-memory dict guarded by a lock. That is a real
limitation -- state is lost on restart and this does not scale past one
worker process -- but this is a local single-user POC (see the effort
estimate in verify.md: Celery/RQ + Redis is future work, not a Stage 2
prerequisite), and ``get_status`` falls back to checking the output file on
disk so a restart at worst forgets an in-flight render, not a finished one.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.models.layout_schema import UnifiedLayout

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RENDER_SCRIPT = Path(__file__).resolve().parent.parent / "blender" / "render_scene.py"
_HDRI_PATH = Path(__file__).resolve().parent.parent / "static" / "hdri" / "phone_shop_1k.hdr"
_DEFAULT_BLENDER_BIN = _REPO_ROOT / ".tools" / "blender" / "blender"


class BlenderNotAvailable(RuntimeError):
    """No usable Blender binary. Run scripts/setup_blender.sh or set BLENDER_BIN."""


class RenderAlreadyRunning(RuntimeError):
    """A render is already in flight for this project."""


class TurntableAlreadyRunning(RuntimeError):
    """A turntable render is already in flight for this project."""


@dataclass
class _Job:
    status: str  # "running" | "done" | "error"
    started_at: float
    finished_at: float | None = None
    error: str | None = None
    samples: int = 0
    width: int = 0
    height: int = 0
    viewpoint_id: str = "PLAN"
    process: subprocess.Popen | None = field(default=None, repr=False)


@dataclass
class _TurntableJob:
    status: str  # "running" | "done" | "error" | "cancelled"
    started_at: float
    finished_at: float | None = None
    error: str | None = None
    frames_total: int = 0
    frames_done: int = 0
    samples: int = 0
    width: int = 0
    height: int = 0
    cancel_requested: bool = False
    process: subprocess.Popen | None = field(default=None, repr=False)


_jobs: dict[str, _Job] = {}
_lock = threading.Lock()

_turntable_jobs: dict[str, _TurntableJob] = {}
_turntable_lock = threading.Lock()


def resolve_blender_bin() -> Path | None:
    """``BLENDER_BIN``/settings override first, else the portable install
    scripts/setup_blender.sh fetches. Returns None (never raises) so callers
    decide how to surface "not installed"."""
    configured = get_settings().blender_bin
    candidate = Path(configured) if configured else _DEFAULT_BLENDER_BIN
    if candidate.is_file():
        return candidate
    return None


def render_output_dir(project_id: str) -> Path:
    return get_settings().output_dir / project_id


def render_glb_path(project_id: str) -> Path:
    return render_output_dir(project_id) / "render_scene.glb"


def render_output_path(project_id: str) -> Path:
    return render_output_dir(project_id) / "render.png"


def render_log_path(project_id: str) -> Path:
    return render_output_dir(project_id) / "render.log"


def render_meta_path(project_id: str) -> Path:
    """Small sidecar next to render.png — just enough (viewpoint_id, samples,
    size) to answer get_status correctly after a restart, when the in-memory
    job record that produced the PNG is gone but the PNG itself is not."""
    return render_output_dir(project_id) / "render.json"


def turntable_frame_dir(project_id: str) -> Path:
    return render_output_dir(project_id) / "turntable_frames"


def turntable_output_path(project_id: str) -> Path:
    return render_output_dir(project_id) / "turntable.gif"


def turntable_log_path(project_id: str) -> Path:
    return render_output_dir(project_id) / "turntable.log"


def turntable_meta_path(project_id: str) -> Path:
    return render_output_dir(project_id) / "turntable.json"


def _pick_viewpoint(
    viewpoints: list[dict[str, Any]], viewpoint_id: str | None = None
) -> dict[str, Any]:
    """
    Same named viewpoints the browser viewer's dropdown offers
    (``build_viewpoints``, the DXF exporter's A-D markers). Falls back to
    "PLAN" -- the default the 3D Massing tab itself opens on -- if the
    requested id doesn't exist on this layout, and to the first available
    viewpoint if even that is missing, rather than raising: a bad or stale
    id from the client should degrade to *some* real view, not fail the render.
    """
    if viewpoint_id:
        for vp in viewpoints:
            if vp.get("id") == viewpoint_id:
                return vp
    for vp in viewpoints:
        if vp.get("id") == "PLAN":
            return vp
    if viewpoints:
        return viewpoints[0]
    # build_viewpoints always returns at least the PLAN entry (see
    # mesh_builder._plan_viewpoint); this is a defensive fallback only, not a
    # path any current layout can reach.
    return {"id": "PLAN", "position": [0, -5000, 3000], "target": [0, 0, 1000], "orthographic": True}


def _build_blender_cmd(
    blender_bin: Path,
    glb_path: Path,
    out_png: Path,
    viewpoint: dict[str, Any],
    samples: int,
    width: int,
    height: int,
) -> list[str]:
    cam = viewpoint.get("position", [0, -5000, 3000])
    target = viewpoint.get("target", [0, 0, 1000])
    orthographic = bool(viewpoint.get("orthographic", False))
    # Mirrors the GLB viewer's own show_groups/hide_groups convention (see
    # mesh_builder.build_viewpoints / massing-viewer.js applyViewpoint): the
    # PLAN/A-D "doll's-house" views stand outside looking in, where an open
    # ceiling is the point; the eye-level ENTRANCE/ANGLED/REAR presets stand
    # inside the room, where a real customer would see a ceiling overhead.
    show_ceiling = "ceiling" in viewpoint.get("show_groups", [])

    cmd = [
        str(blender_bin),
        "--background",
        "--python",
        str(_RENDER_SCRIPT),
        "--",
        str(glb_path),
        str(_HDRI_PATH),
        str(out_png),
        "--samples",
        str(samples),
        "--width",
        str(width),
        "--height",
        str(height),
        "--cam",
        str(cam[0]),
        str(cam[1]),
        str(cam[2]),
        "--target",
        str(target[0]),
        str(target[1]),
        str(target[2]),
    ]
    if orthographic:
        cmd.append("--orthographic")
    if show_ceiling:
        cmd.append("--show-ceiling")
    return cmd


def _run_blender(
    blender_bin: Path,
    project_id: str,
    glb_path: Path,
    out_png: Path,
    log_path: Path,
    meta_path: Path,
    viewpoint: dict[str, Any],
    samples: int,
    width: int,
    height: int,
) -> None:
    """Blocking. Called on a background thread by start_render."""
    cmd = _build_blender_cmd(blender_bin, glb_path, out_png, viewpoint, samples, width, height)

    t0 = time.time()
    with _lock:
        job = _jobs[project_id]

    try:
        with open(log_path, "w", encoding="utf-8") as log_f:
            log_f.write(f"$ {' '.join(cmd)}\n\n")
            log_f.flush()
            proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
            with _lock:
                job.process = proc
            timeout = get_settings().photoreal_timeout_seconds
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise RuntimeError(f"render exceeded {timeout:.0f}s and was killed") from None

        if returncode != 0:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                pass
            raise RuntimeError(f"blender exited {returncode}. Log tail:\n{tail}")

        if not out_png.is_file():
            raise RuntimeError("blender exited 0 but wrote no output image")

        with _lock:
            job.status = "done"
            job.finished_at = time.time()
            meta_path.write_text(
                json.dumps(
                    {
                        "viewpoint_id": job.viewpoint_id,
                        "samples": job.samples,
                        "width": job.width,
                        "height": job.height,
                    }
                ),
                encoding="utf-8",
            )
        logger.info(
            "photoreal render done project_id=%s viewpoint=%s elapsed=%.1fs",
            project_id, job.viewpoint_id, time.time() - t0,
        )
    except Exception as exc:
        logger.exception("photoreal render failed project_id=%s", project_id)
        with _lock:
            job.status = "error"
            job.finished_at = time.time()
            job.error = str(exc)


def start_render(
    project_id: str, layout: UnifiedLayout, viewpoint_id: str | None = None
) -> dict[str, Any]:
    """
    Kick off a background render for ``project_id``. Returns immediately with
    a status dict; poll ``get_status`` for completion.

    Builds the GLB itself (same ``build_massing`` the /glb route uses) rather
    than taking bytes from the caller, so the render always reflects the
    latest layout, matching the "regenerated on demand, nothing cached
    server-side by default" stance the /glb route already documents.

    ``viewpoint_id`` picks from the same named viewpoints the browser
    viewer's dropdown offers (see ``_pick_viewpoint``); ``None`` or an id this
    layout doesn't have both fall back to "PLAN". Only one render at a time
    per project -- switching viewpoints re-renders and replaces the previous
    PNG rather than keeping one cached per angle, which keeps this simple at
    the cost of a wasted render if you switch back and forth.
    """
    blender_bin = resolve_blender_bin()
    if blender_bin is None:
        raise BlenderNotAvailable(
            "No Blender binary found. Run `bash scripts/setup_blender.sh` "
            "or set BLENDER_BIN / settings.blender_bin to an existing install."
        )

    with _lock:
        existing = _jobs.get(project_id)
        if existing is not None and existing.status == "running":
            raise RenderAlreadyRunning(f"A render for project {project_id} is already running.")

    from app.services.mesh_builder import EmptyMassingError, build_massing

    try:
        result = build_massing(layout)
        glb_bytes = result.export_glb()
    except EmptyMassingError as exc:
        raise ValueError(str(exc)) from exc

    viewpoint = _pick_viewpoint(result.viewpoints, viewpoint_id)
    resolved_viewpoint_id = str(viewpoint.get("id") or "PLAN")
    settings = get_settings()

    out_dir = render_output_dir(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = render_glb_path(project_id)
    glb_path.write_bytes(glb_bytes)
    out_png = render_output_path(project_id)
    log_path = render_log_path(project_id)
    meta_path = render_meta_path(project_id)
    if out_png.is_file():
        out_png.unlink()  # stale render from a previous layout must not look current
    if meta_path.is_file():
        meta_path.unlink()

    job = _Job(
        status="running",
        started_at=time.time(),
        samples=settings.photoreal_samples,
        width=settings.photoreal_width,
        height=settings.photoreal_height,
        viewpoint_id=resolved_viewpoint_id,
    )
    with _lock:
        _jobs[project_id] = job

    thread = threading.Thread(
        target=_run_blender,
        args=(
            blender_bin,
            project_id,
            glb_path,
            out_png,
            log_path,
            meta_path,
            viewpoint,
            job.samples,
            job.width,
            job.height,
        ),
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "project_id": project_id,
        "samples": job.samples,
        "width": job.width,
        "height": job.height,
        "viewpoint_id": job.viewpoint_id,
    }


def get_status(project_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(project_id)

    if job is None:
        # No record in this process (fresh start, or a restart lost it) --
        # fall back to whether a finished render is sitting on disk, and to
        # its sidecar metadata for which viewpoint it was.
        if render_output_path(project_id).is_file():
            out: dict[str, Any] = {
                "status": "done", "project_id": project_id, "restored_from_disk": True,
            }
            meta_path = render_meta_path(project_id)
            if meta_path.is_file():
                try:
                    out.update(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    pass
            return out
        return {"status": "not_started", "project_id": project_id}

    out = {
        "status": job.status,
        "project_id": project_id,
        "samples": job.samples,
        "width": job.width,
        "height": job.height,
        "viewpoint_id": job.viewpoint_id,
        "elapsed_s": round((job.finished_at or time.time()) - job.started_at, 1),
    }
    if job.error:
        out["error"] = job.error
    return out


# ---------------------------------------------------------------------------
# Turntable: a pre-rendered 360-degree lap, not a live orbit
#
# There is no hardware path on this machine to an orbit-able *photoreal*
# view -- that needs real-time ray tracing, which needs a GPU class this
# CPU-only box doesn't have (see the 3D Massing tab for the real orbit; it
# trades photoreal lighting for speed to get there). What CPU rendering can
# do instead is pre-compute a fixed sequence of stills around a full circle,
# once, and play them back as a loop -- a canned rotation to watch, not
# something you steer. Stitched with Pillow (already a dependency) into an
# animated GIF: no ffmpeg dependency, and <img src=...gif> autoplays and
# loops in every browser with no JavaScript at all.
# ---------------------------------------------------------------------------


def _stitch_gif(frame_paths: list[Path], out_gif: Path, frame_ms: int = 110) -> None:
    from PIL import Image

    images = [Image.open(p).convert("RGB") for p in frame_paths]
    if not images:
        raise RuntimeError("no frames to stitch")
    images[0].save(
        out_gif,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=frame_ms,
        loop=0,
        optimize=True,
    )


def _run_turntable(
    blender_bin: Path,
    project_id: str,
    glb_path: Path,
    frame_dir: Path,
    log_path: Path,
    meta_path: Path,
    out_gif: Path,
    viewpoints: list[dict[str, Any]],
    samples: int,
    width: int,
    height: int,
    timeout_per_frame: float,
) -> None:
    """Blocking. Called on a background thread by start_turntable. Renders
    each frame as its own Blender subprocess (simpler and safer than trying
    to keep one Blender process warm across frames, at the cost of a
    negligible ~0.2s/frame startup overhead -- see render_scene.py's own
    measured setup time)."""
    t0 = time.time()
    with _turntable_lock:
        job = _turntable_jobs[project_id]

    try:
        frame_paths: list[Path] = []
        log_path.write_text("", encoding="utf-8")  # fresh log for this run
        for i, vp in enumerate(viewpoints):
            with _turntable_lock:
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    return

            frame_path = frame_dir / f"frame_{i:03d}.png"
            cmd = _build_blender_cmd(blender_bin, glb_path, frame_path, vp, samples, width, height)

            with open(log_path, "a", encoding="utf-8") as log_f:
                log_f.write(f"\n--- frame {i + 1}/{len(viewpoints)} ---\n$ {' '.join(cmd)}\n")
                log_f.flush()
                proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
                with _turntable_lock:
                    job.process = proc
                try:
                    returncode = proc.wait(timeout=timeout_per_frame)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(
                        f"frame {i} exceeded {timeout_per_frame:.0f}s and was killed"
                    ) from None

            with _turntable_lock:
                cancelled = job.cancel_requested
            if cancelled:
                # A cancel between "wait started" and "wait returned" kills
                # this frame's process (see cancel_turntable), which makes
                # returncode nonzero too -- check the flag first so that
                # reads as a clean cancel, not a render failure.
                with _turntable_lock:
                    job.status = "cancelled"
                    job.finished_at = time.time()
                return

            if returncode != 0:
                tail = ""
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                except OSError:
                    pass
                raise RuntimeError(f"frame {i} exited {returncode}. Log tail:\n{tail}")
            if not frame_path.is_file():
                raise RuntimeError(f"frame {i}: blender exited 0 but wrote no image")

            frame_paths.append(frame_path)
            with _turntable_lock:
                job.frames_done = i + 1

        _stitch_gif(frame_paths, out_gif)

        with _turntable_lock:
            job.status = "done"
            job.finished_at = time.time()
            meta_path.write_text(
                json.dumps(
                    {"frames": job.frames_total, "samples": job.samples,
                     "width": job.width, "height": job.height}
                ),
                encoding="utf-8",
            )
        logger.info(
            "turntable render done project_id=%s frames=%d elapsed=%.1fs",
            project_id, job.frames_total, time.time() - t0,
        )
    except Exception as exc:
        logger.exception("turntable render failed project_id=%s", project_id)
        with _turntable_lock:
            job.status = "error"
            job.finished_at = time.time()
            job.error = str(exc)


def start_turntable(project_id: str, layout: UnifiedLayout) -> dict[str, Any]:
    """
    Kick off a background turntable render: a fixed lap of stills around the
    layout, later stitched into a looping GIF. See the module comment above
    for why this exists instead of a live orbit.

    Frame count/samples/resolution come from settings
    (photoreal_turntable_*), not from the caller -- unlike a single still,
    which already lets the viewpoint vary per request, a turntable's shape
    (how many frames, how heavy each one is) is a server-side cost decision,
    not something to expose per click.
    """
    blender_bin = resolve_blender_bin()
    if blender_bin is None:
        raise BlenderNotAvailable(
            "No Blender binary found. Run `bash scripts/setup_blender.sh` "
            "or set BLENDER_BIN / settings.blender_bin to an existing install."
        )

    with _turntable_lock:
        existing = _turntable_jobs.get(project_id)
        if existing is not None and existing.status == "running":
            raise TurntableAlreadyRunning(
                f"A turntable render for project {project_id} is already running."
            )

    from app.services.mesh_builder import (
        EmptyMassingError,
        build_massing,
        resolve_ceiling_height_mm,
        turntable_viewpoints,
    )

    try:
        result = build_massing(layout)
        glb_bytes = result.export_glb()
    except EmptyMassingError as exc:
        raise ValueError(str(exc)) from exc

    settings = get_settings()
    n_frames = max(2, settings.photoreal_turntable_frames)
    ceiling_mm, _ = resolve_ceiling_height_mm(layout)
    viewpoints = turntable_viewpoints(layout, ceiling_mm, n_frames)
    if not viewpoints:
        raise ValueError("Layout has no extent to build a turntable around.")

    out_dir = render_output_dir(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    glb_path = render_glb_path(project_id)
    glb_path.write_bytes(glb_bytes)

    frame_dir = turntable_frame_dir(project_id)
    frame_dir.mkdir(parents=True, exist_ok=True)
    for stale in frame_dir.glob("frame_*.png"):
        stale.unlink()

    out_gif = turntable_output_path(project_id)
    log_path = turntable_log_path(project_id)
    meta_path = turntable_meta_path(project_id)
    if out_gif.is_file():
        out_gif.unlink()  # stale animation from a previous layout must not look current
    if meta_path.is_file():
        meta_path.unlink()

    job = _TurntableJob(
        status="running",
        started_at=time.time(),
        frames_total=len(viewpoints),
        samples=settings.photoreal_turntable_samples,
        width=settings.photoreal_turntable_width,
        height=settings.photoreal_turntable_height,
    )
    with _turntable_lock:
        _turntable_jobs[project_id] = job

    thread = threading.Thread(
        target=_run_turntable,
        args=(
            blender_bin,
            project_id,
            glb_path,
            frame_dir,
            log_path,
            meta_path,
            out_gif,
            viewpoints,
            job.samples,
            job.width,
            job.height,
            settings.photoreal_turntable_frame_timeout_seconds,
        ),
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "project_id": project_id,
        "frames_total": job.frames_total,
        "samples": job.samples,
        "width": job.width,
        "height": job.height,
    }


def get_turntable_status(project_id: str) -> dict[str, Any]:
    with _turntable_lock:
        job = _turntable_jobs.get(project_id)

    if job is None:
        if turntable_output_path(project_id).is_file():
            out: dict[str, Any] = {
                "status": "done", "project_id": project_id, "restored_from_disk": True,
            }
            meta_path = turntable_meta_path(project_id)
            if meta_path.is_file():
                try:
                    out.update(json.loads(meta_path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    pass
            return out
        return {"status": "not_started", "project_id": project_id}

    out = {
        "status": job.status,
        "project_id": project_id,
        "frames_total": job.frames_total,
        "frames_done": job.frames_done,
        "samples": job.samples,
        "width": job.width,
        "height": job.height,
        "elapsed_s": round((job.finished_at or time.time()) - job.started_at, 1),
    }
    if job.error:
        out["error"] = job.error
    return out


def cancel_turntable(project_id: str) -> dict[str, Any]:
    """
    Best-effort: sets a flag the render loop checks between frames (so it
    never leaves a half-written frame counted as done), and also kills the
    frame currently in flight rather than waiting out the rest of its
    timeout -- a 20+ minute job needs a real cancel, not just "stop queuing
    more work."
    """
    with _turntable_lock:
        job = _turntable_jobs.get(project_id)
        if job is None or job.status != "running":
            return {"status": "not_running", "project_id": project_id}
        job.cancel_requested = True
        proc = job.process
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    return {"status": "cancelling", "project_id": project_id}
