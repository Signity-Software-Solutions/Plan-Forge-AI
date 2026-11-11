"""
Unit tests for app.services.photoreal — the orchestration layer only.

Never launches real Blender: subprocess.Popen and threading.Thread are both
faked so tests are fast and deterministic. app/blender/render_scene.py itself
is exercised by hand (see the render probe used during development), not by
this suite — it needs bpy, which is not installed in this venv by design.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.models.layout_schema import UnifiedLayout
from app.services import photoreal


def _room_layout() -> UnifiedLayout:
    return UnifiedLayout(
        units="mm",
        walls=[
            {"x1": 0, "y1": 0, "x2": 6000, "y2": 0},
            {"x1": 6000, "y1": 0, "x2": 6000, "y2": 4000},
            {"x1": 6000, "y1": 4000, "x2": 0, "y2": 4000},
            {"x1": 0, "y1": 4000, "x2": 0, "y2": 0},
        ],
        rooms=[{"name": "Retail", "area": 24, "polygon": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]}],
        fixtures=[
            {"kind": "display", "x": 1500, "y": 2000, "width_mm": 1200, "depth_mm": 600,
             "height_mm": 1800, "label": "LFD"},
        ],
    )


@pytest.fixture(autouse=True)
def _isolated_jobs(monkeypatch):
    """Every test gets a clean job registry -- it's process-global by design."""
    monkeypatch.setattr(photoreal, "_jobs", {})
    monkeypatch.setattr(photoreal, "_turntable_jobs", {})
    yield


@pytest.fixture
def _sync_thread(monkeypatch):
    """
    Run the "background" render thread synchronously and in-process, so a
    test can assert on job state right after start_render() returns instead
    of racing a real thread.
    """

    class _ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(photoreal.threading, "Thread", _ImmediateThread)


class _FakePopen:
    """Stands in for subprocess.Popen. Writes the expected output file and
    reports success, unless told to fail."""

    def __init__(self, cmd, stdout=None, stderr=None, *, returncode=0, out_png=None):
        self.cmd = cmd
        self._returncode = returncode
        self._out_png = out_png
        if stdout:
            stdout.write("fake blender run\n")

    def wait(self, timeout=None):
        if self._returncode == 0 and self._out_png is not None:
            # A real (tiny) PNG, not just PNG-shaped bytes: _stitch_gif opens
            # every frame with PIL, so a turntable test needs an image PIL
            # can actually decode, not merely a file that exists. Colour
            # varies by filename so a run of frames isn't bit-identical --
            # Pillow's GIF writer collapses genuinely identical consecutive
            # frames when optimize=True, which would make a frame-count
            # assertion fail for reasons that have nothing to do with the
            # code under test.
            from PIL import Image

            seed = sum(self._out_png.name.encode("utf-8")) % 200
            Image.new("RGB", (4, 3), color=(seed, 20, 30)).save(self._out_png)
        return self._returncode

    def kill(self):
        pass


def test_resolve_blender_bin_returns_none_when_nothing_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(photoreal, "_DEFAULT_BLENDER_BIN", tmp_path / "no-such-blender")
    monkeypatch.setattr(photoreal.get_settings(), "blender_bin", None)
    assert photoreal.resolve_blender_bin() is None


def test_resolve_blender_bin_finds_a_configured_path(monkeypatch, tmp_path):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal.get_settings(), "blender_bin", str(fake_bin))
    assert photoreal.resolve_blender_bin() == fake_bin


def test_start_render_refuses_without_a_blender_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: None)
    with pytest.raises(photoreal.BlenderNotAvailable):
        photoreal.start_render("proj-1", _room_layout())


def test_start_render_refuses_a_second_concurrent_job(monkeypatch, tmp_path):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    photoreal._jobs["proj-1"] = photoreal._Job(status="running", started_at=time.time())
    with pytest.raises(photoreal.RenderAlreadyRunning):
        photoreal.start_render("proj-1", _room_layout())


def test_start_render_happy_path_writes_glb_and_reports_done(
    monkeypatch, tmp_path, _sync_thread
):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")

    captured_cmd = {}

    def fake_popen(cmd, stdout=None, stderr=None):
        captured_cmd["cmd"] = cmd
        out_png = photoreal.render_output_path("proj-1")
        return _FakePopen(cmd, stdout=stdout, stderr=stderr, returncode=0, out_png=out_png)

    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    result = photoreal.start_render("proj-1", _room_layout())
    assert result["status"] == "started"

    # The synchronous-thread fixture means the "background" job has already
    # run to completion by the time start_render returns.
    status = photoreal.get_status("proj-1")
    assert status["status"] == "done"
    assert photoreal.render_output_path("proj-1").is_file()
    assert photoreal.render_glb_path("proj-1").is_file()

    # The camera args came from the layout's own PLAN viewpoint, not a stub.
    cmd = captured_cmd["cmd"]
    assert "--cam" in cmd
    assert "--target" in cmd


def test_start_render_reports_error_status_on_a_nonzero_exit(monkeypatch, tmp_path, _sync_thread):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")

    def fake_popen(cmd, stdout=None, stderr=None):
        return _FakePopen(cmd, stdout=stdout, stderr=stderr, returncode=1, out_png=None)

    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    photoreal.start_render("proj-1", _room_layout())
    status = photoreal.get_status("proj-1")
    assert status["status"] == "error"
    assert "1" in status["error"]


def test_get_status_reports_not_started_with_no_job_and_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    assert photoreal.get_status("nobody-home")["status"] == "not_started"


def test_get_status_falls_back_to_disk_after_a_restart(tmp_path, monkeypatch):
    """
    The job dict is process-local; a restart between "render finished" and
    "user checks status" must not report failure when the PNG is right there.
    """
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    out = photoreal.render_output_path("proj-1")
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    status = photoreal.get_status("proj-1")
    assert status["status"] == "done"
    assert status["restored_from_disk"] is True


def test_pick_viewpoint_prefers_plan_then_first_then_a_safe_default():
    assert photoreal._pick_viewpoint([{"id": "A"}, {"id": "PLAN", "marker": 1}]) == {
        "id": "PLAN",
        "marker": 1,
    }
    assert photoreal._pick_viewpoint([{"id": "A", "marker": 2}])["marker"] == 2
    default = photoreal._pick_viewpoint([])
    assert "position" in default and "target" in default


def test_pick_viewpoint_honours_an_explicit_id():
    viewpoints = [{"id": "PLAN", "marker": 0}, {"id": "B", "marker": 2}]
    assert photoreal._pick_viewpoint(viewpoints, "B")["marker"] == 2


def test_pick_viewpoint_falls_back_to_plan_for_an_unknown_id():
    """A stale or bad id from the client degrades to a real view, not a 404
    or a render of nothing."""
    viewpoints = [{"id": "PLAN", "marker": 0}, {"id": "B", "marker": 2}]
    assert photoreal._pick_viewpoint(viewpoints, "does-not-exist")["marker"] == 0


def test_start_render_honours_the_requested_viewpoint(monkeypatch, tmp_path, _sync_thread):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")

    def fake_popen(cmd, stdout=None, stderr=None):
        out_png = photoreal.render_output_path("proj-1")
        return _FakePopen(cmd, stdout=stdout, stderr=stderr, returncode=0, out_png=out_png)

    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    result = photoreal.start_render("proj-1", _room_layout(), viewpoint_id="B")
    assert result["viewpoint_id"] == "B"
    status = photoreal.get_status("proj-1")
    assert status["viewpoint_id"] == "B"


def test_an_unrecognised_viewpoint_id_falls_back_to_plan_rather_than_failing(
    monkeypatch, tmp_path, _sync_thread
):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")

    def fake_popen(cmd, stdout=None, stderr=None):
        out_png = photoreal.render_output_path("proj-1")
        return _FakePopen(cmd, stdout=stdout, stderr=stderr, returncode=0, out_png=out_png)

    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    result = photoreal.start_render("proj-1", _room_layout(), viewpoint_id="not-a-real-id")
    assert result["viewpoint_id"] == "PLAN"


def test_the_rendered_viewpoint_survives_a_restart_via_the_sidecar_file(tmp_path, monkeypatch):
    """
    get_status's disk-restore path (job dict gone, PNG still on disk) must
    also recover which viewpoint that PNG is -- not just that a render
    exists -- or a restart silently mislabels the image.
    """
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    out = photoreal.render_output_path("proj-1")
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    photoreal.render_meta_path("proj-1").write_text('{"viewpoint_id": "C", "samples": 64}')

    status = photoreal.get_status("proj-1")
    assert status["status"] == "done"
    assert status["viewpoint_id"] == "C"


# ---------------------------------------------------------------------------
# Turntable: a pre-rendered lap, not a live orbit
# ---------------------------------------------------------------------------


def _frame_popen_factory(fail_on_frame=None, fail_returncode=1):
    """Builds a fake subprocess.Popen for the turntable path: each call
    writes the frame PNG named in its own --cam-preceding out_png argument
    (cmd[7] in _build_blender_cmd's fixed argument order), so N sequential
    calls produce N distinct frame files, the way N real Blender subprocess
    calls would. Every frame succeeds (rc=0) except ``fail_on_frame``
    (0-indexed), which reports ``fail_returncode``."""
    calls = {"n": 0}

    def factory(cmd, stdout=None, stderr=None):
        calls["n"] += 1
        idx = calls["n"] - 1
        out_png = Path(cmd[7])
        rc = fail_returncode if fail_on_frame is not None and idx == fail_on_frame else 0
        return _FakePopen(cmd, stdout=stdout, stderr=stderr, returncode=rc, out_png=out_png)

    return factory, calls


def test_start_turntable_refuses_without_a_blender_binary(monkeypatch):
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: None)
    with pytest.raises(photoreal.BlenderNotAvailable):
        photoreal.start_turntable("proj-1", _room_layout())


def test_start_turntable_refuses_a_second_concurrent_job(monkeypatch, tmp_path):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    photoreal._turntable_jobs["proj-1"] = photoreal._TurntableJob(
        status="running", started_at=time.time()
    )
    with pytest.raises(photoreal.TurntableAlreadyRunning):
        photoreal.start_turntable("proj-1", _room_layout())


def test_start_turntable_renders_every_frame_and_stitches_a_gif(monkeypatch, tmp_path, _sync_thread):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    monkeypatch.setattr(photoreal.get_settings(), "photoreal_turntable_frames", 6)
    monkeypatch.setattr(photoreal.get_settings(), "photoreal_turntable_width", 16)
    monkeypatch.setattr(photoreal.get_settings(), "photoreal_turntable_height", 12)

    fake_popen, calls = _frame_popen_factory()
    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    result = photoreal.start_turntable("proj-1", _room_layout())
    assert result["status"] == "started"
    assert result["frames_total"] == 6

    status = photoreal.get_status  # noqa: F841 (sanity that we didn't shadow the wrong function)
    tt_status = photoreal.get_turntable_status("proj-1")
    assert tt_status["status"] == "done"
    assert tt_status["frames_done"] == 6
    assert calls["n"] == 6

    gif_path = photoreal.turntable_output_path("proj-1")
    assert gif_path.is_file()
    from PIL import Image

    with Image.open(gif_path) as gif:
        assert getattr(gif, "n_frames", 1) == 6


def test_start_turntable_reports_error_on_a_failed_frame_without_finishing_the_rest(
    monkeypatch, tmp_path, _sync_thread
):
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    monkeypatch.setattr(photoreal.get_settings(), "photoreal_turntable_frames", 6)

    fake_popen, calls = _frame_popen_factory(fail_on_frame=2)
    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    photoreal.start_turntable("proj-1", _room_layout())
    status = photoreal.get_turntable_status("proj-1")
    assert status["status"] == "error"
    assert "2" in status["error"]
    # Frame 2 (0-indexed) failed, so frames 3-6 must never have been asked for.
    assert calls["n"] == 3
    assert not photoreal.turntable_output_path("proj-1").is_file()


def test_cancel_turntable_stops_before_finishing_all_frames(monkeypatch, tmp_path):
    """
    Cancellation is checked between frames (and kills the in-flight one), so
    with a real (non-synchronous) thread the job should stop partway rather
    than silently running to completion once cancelled.
    """
    fake_bin = tmp_path / "blender"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(photoreal, "resolve_blender_bin", lambda: fake_bin)
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    monkeypatch.setattr(photoreal.get_settings(), "photoreal_turntable_frames", 6)

    release = threading.Event()

    class _SlowPopen(_FakePopen):
        def wait(self, timeout=None):
            release.wait(timeout=5)
            return super().wait(timeout=timeout)

    def fake_popen(cmd, stdout=None, stderr=None):
        out_png = Path(cmd[7])
        return _SlowPopen(cmd, stdout=stdout, stderr=stderr, returncode=0, out_png=out_png)

    monkeypatch.setattr(photoreal.subprocess, "Popen", fake_popen)

    result = photoreal.start_turntable("proj-1", _room_layout())
    assert result["status"] == "started"

    cancel_result = photoreal.cancel_turntable("proj-1")
    assert cancel_result["status"] == "cancelling"
    release.set()

    deadline = time.time() + 5
    status = photoreal.get_turntable_status("proj-1")
    while status["status"] == "running" and time.time() < deadline:
        time.sleep(0.05)
        status = photoreal.get_turntable_status("proj-1")
    assert status["status"] == "cancelled"
    assert status["frames_done"] < status["frames_total"]


def test_cancel_turntable_when_nothing_is_running_is_a_no_op():
    assert photoreal.cancel_turntable("nobody-home")["status"] == "not_running"


def test_get_turntable_status_reports_not_started_with_no_job_and_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    assert photoreal.get_turntable_status("nobody-home")["status"] == "not_started"


def test_get_turntable_status_falls_back_to_disk_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(photoreal.get_settings(), "output_dir", tmp_path / "outputs")
    gif = photoreal.turntable_output_path("proj-1")
    gif.parent.mkdir(parents=True)
    gif.write_bytes(b"GIF89a fake")
    photoreal.turntable_meta_path("proj-1").write_text('{"frames": 24, "samples": 48}')

    status = photoreal.get_turntable_status("proj-1")
    assert status["status"] == "done"
    assert status["restored_from_disk"] is True
    assert status["frames"] == 24


def test_stitch_gif_produces_a_valid_looping_animation(tmp_path):
    from PIL import Image

    frame_paths = []
    for i in range(4):
        p = tmp_path / f"frame_{i:03d}.png"
        Image.new("RGB", (8, 6), color=(i * 40, 0, 0)).save(p)
        frame_paths.append(p)

    out_gif = tmp_path / "out.gif"
    photoreal._stitch_gif(frame_paths, out_gif)

    assert out_gif.is_file()
    with Image.open(out_gif) as gif:
        assert gif.n_frames == 4
        assert gif.info.get("loop") == 0  # loop forever, not once


def test_build_blender_cmd_passes_show_ceiling_for_eye_level_viewpoints():
    """Mirrors the GLB viewer's viewpoint.show_groups convention (see
    mesh_builder.build_viewpoints / massing-viewer.js applyViewpoint) --
    an eye-level interior preset should render with its ceiling, not the
    PLAN/A-D "doll's-house" cutaway's hidden one."""
    interior_vp = {"position": [0, 0, 1650], "target": [1000, 0, 1650], "show_groups": ["ceiling"]}
    cutaway_vp = {"position": [0, -5000, 3000], "target": [0, 0, 1000]}

    cmd_interior = photoreal._build_blender_cmd(
        Path("/blender"), Path("/in.glb"), Path("/out.png"), interior_vp, 64, 800, 600
    )
    cmd_cutaway = photoreal._build_blender_cmd(
        Path("/blender"), Path("/in.glb"), Path("/out.png"), cutaway_vp, 64, 800, 600
    )

    assert "--show-ceiling" in cmd_interior
    assert "--show-ceiling" not in cmd_cutaway
