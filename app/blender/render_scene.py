"""
Photoreal still render, Stage 2 of the massing -> photoreal path.

Runs ONLY inside Blender's own Python (``blender --background --python
render_scene.py -- <args>``), invoked as a subprocess by
``app.services.photoreal``. This file is never imported by the FastAPI app:
``bpy`` is not installed in that venv, and Blender is GPL-licensed, so the
boundary is a subprocess call, not a Python import, deliberately.

What it does, mirroring the browser viewer (app/static/js/massing-viewer.js)
so the two stay visually consistent rather than diverging into two different
looks for the same model:

  * Imports the GLB mesh_builder already produces -- same geometry, same
    materials (colour + ORM + normal maps for floor/wall), nothing rebuilt.
  * Hides the ceiling, matching the viewer's DEFAULT_HIDDEN = ["ceiling"].
  * Lights with the same CC0 HDRI (app/static/hdri/), split so it lights and
    reflects the scene but does not appear as a literal photo behind it --
    the viewer keeps scene.background flat and scene.environment separate,
    and the World node graph below reproduces that split for Cycles.
  * Places the camera at the eye/target the server already computed for the
    "PLAN" viewpoint (mesh_builder._plan_viewpoint / build_viewpoints), so the
    photoreal still frames the room the same way the interactive view does.

Unit note: mesh_builder ships raw millimetre numbers as the vertex/position
values (the browser viewer treats them as its own arbitrary units and only
converts them for on-screen dimension labels). glTF's spec says one unit IS
one metre, so Blender's importer takes a "6000" wide room literally as 6000
metres. Fixed by scaling the imported root 0.001 -- and by scaling every
camera coordinate passed in on the command line by the same factor, since
those numbers come from the same millimetre layout.

Lighting assumption, stated rather than hidden: there is no real reflected
ceiling plan in any input this pipeline has -- no downlight positions, no
fixture schedule, nothing a real electrical layout would supply. What this
module builds instead is a *generic* evenly-spaced grid of small ceiling
lights, spaced by a stated constant (``_DOWNLIGHT_SPACING_M``) and derived
only from the imported scene's own floor bounds -- not tied to any real
fixture, store, or client-supplied ceiling design. It reads as "a lit retail
ceiling" the way a real one does (many soft point sources, not one hard sun),
without claiming to be a survey of one. Revisit if/when a real ceiling plan
becomes available.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

import bpy
import mathutils

MM_TO_M = 0.001


def _parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1 :]
    p = argparse.ArgumentParser()
    p.add_argument("glb_path")
    p.add_argument("hdri_path")
    p.add_argument("out_png_path")
    p.add_argument("--samples", type=int, default=128)
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    # Camera eye/target, in the same millimetre layout coordinates as the
    # viewpoint dicts build_viewpoints() emits (position/target, X/Y/Z).
    p.add_argument("--cam", type=float, nargs=3, required=True, metavar=("X", "Y", "Z"))
    p.add_argument("--target", type=float, nargs=3, required=True, metavar=("X", "Y", "Z"))
    p.add_argument("--orthographic", action="store_true")
    p.add_argument("--lens", type=float, default=32.0)
    # Mirrors the GLB viewer's viewpoint.show_groups -- an eye-level interior
    # shot keeps the ceiling; the default "doll's-house" cutaway hides it.
    p.add_argument("--show-ceiling", action="store_true")
    return p.parse_args(argv)


def _clear_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _import_glb(glb_path: str) -> None:
    bpy.ops.import_scene.gltf(filepath=glb_path)


def _hide_ceiling() -> None:
    """Mirror the viewer's DEFAULT_HIDDEN = ["ceiling"] by deleting the group
    outright -- Blender has no per-viewer visibility toggle to reuse."""
    doomed = [o for o in bpy.data.objects if o.name == "ceiling" or o.name.startswith("ceiling_")]
    for obj in doomed:
        bpy.data.objects.remove(obj, do_unlink=True)


def _rescale_to_metres() -> None:
    root = bpy.data.objects.get("PLANFORGE_ROOT")
    if root is not None:
        root.scale = (MM_TO_M, MM_TO_M, MM_TO_M)
    # matrix_world on every child is stale until the depsgraph re-evaluates --
    # skip this and every bbox/placement computed after it silently uses the
    # pre-scale (millimetre) transforms despite .scale already being set.
    bpy.context.view_layer.update()


def _build_world(hdri_path: str) -> None:
    world = bpy.data.worlds.new("PlanforgeWorld")
    bpy.context.scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()

    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(hdri_path)
    bg_env = nt.nodes.new("ShaderNodeBackground")
    bg_env.inputs["Strength"].default_value = 0.6
    nt.links.new(env.outputs["Color"], bg_env.inputs["Color"])

    # Page background (BACKGROUND = 0xf4f4f6 in massing-viewer.js) for direct
    # camera rays; the HDRI stays live for indirect (lighting/reflection) rays
    # via the Mix Shader below -- same background/environment split the
    # browser viewer keeps between scene.background and scene.environment.
    bg_flat = nt.nodes.new("ShaderNodeBackground")
    bg_flat.inputs["Color"].default_value = (0.957, 0.957, 0.961, 1.0)

    light_path = nt.nodes.new("ShaderNodeLightPath")
    mix = nt.nodes.new("ShaderNodeMixShader")
    nt.links.new(light_path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    # Mix Shader: Fac=0 -> input[1], Fac=1 -> input[2]. "Is Camera Ray" is 1
    # for a direct camera ray, so the flat backdrop belongs at input[2].
    nt.links.new(bg_env.outputs["Background"], mix.inputs[1])
    nt.links.new(bg_flat.outputs["Background"], mix.inputs[2])

    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])


def _place_camera(cam_mm: tuple[float, float, float], target_mm: tuple[float, float, float],
                   orthographic: bool, lens_mm: float) -> None:
    scene = bpy.context.scene
    cam_loc = mathutils.Vector(cam_mm) * MM_TO_M
    target = mathutils.Vector(target_mm) * MM_TO_M

    cam_data = bpy.data.cameras.new("PlanforgeCam")
    cam_obj = bpy.data.objects.new("PlanforgeCam", cam_data)
    scene.collection.objects.link(cam_obj)
    scene.camera = cam_obj

    cam_obj.location = cam_loc
    direction = target - cam_loc
    cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_start = 0.01
    cam_data.clip_end = 10000.0

    if orthographic:
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = direction.length * 0.9
    else:
        cam_data.lens = lens_mm


_DOWNLIGHT_SPACING_M = 3.0  # stated assumption, not measured -- see module docstring
_DOWNLIGHT_INSET_M = 0.5    # keep the grid off the walls, like a real reflected plan would
_DOWNLIGHT_SIZE_M = 0.45
_DOWNLIGHT_ENERGY_W = 420.0
_DOWNLIGHT_DROP_BELOW_CEILING_M = 0.08
# Cycles' per-sample cost scales with light count (each one enters the light
# BVH/importance tree), not just scene complexity -- measured directly: the
# first version of this spaced lights every 1.4m (dense enough to double as
# a literal reflected ceiling plan) and cost ~4-5x a single-sun render on the
# reconstructed reference store layout (285s vs. the ~40-65s baseline in
# ARCH.md/PLANFORGE.md, at the same 128 samples). 3.0m spacing with fewer,
# larger, brighter disks keeps the same "soft multi-point ceiling" read at a
# render-time cost this pipeline can actually afford; see PLANFORGE.md/the
# final report for the re-measured number.


def _floor_bounds_m() -> tuple[float, float, float, float, float] | None:
    """
    (x_min, x_max, y_min, y_max, z_max) of everything imported, in metres,
    post-rescale. ``z_max`` doubles as "just under the ceiling" -- on the
    default (ceiling hidden) path the group is already deleted by
    ``_hide_ceiling`` by the time this runs, so the height is recovered from
    the walls' own top edge instead of a second lookup; when ``--show-
    ceiling`` keeps it, its own top surface is simply the same value.

    None for an empty/degenerate import -- callers skip the light grid rather
    than divide by a zero-size room.
    """
    objs = [o for o in bpy.data.objects if o.type == "MESH"]
    if not objs:
        return None
    corners = [o.matrix_world @ mathutils.Vector(c) for o in objs for c in o.bound_box]
    xs, ys, zs = [c.x for c in corners], [c.y for c in corners], [c.z for c in corners]
    return min(xs), max(xs), min(ys), max(ys), max(zs)


def _add_lights() -> None:
    """
    Soft, low fill sun plus a procedural ceiling downlight grid -- a real
    retail ceiling reads as many soft sources, not one hard directional one,
    and a single sun at a raking angle is what was producing harsh,
    blotchy-looking shading on the normal-mapped wall texture (measured by
    rendering and looking, not guessed). The sun stays, much dimmer, purely
    as ambient fill so nothing is ever lit by the downlight grid alone.
    """
    sun_data = bpy.data.lights.new("FillLight", type="SUN")
    sun_data.energy = 0.4
    sun_obj = bpy.data.objects.new("FillLight", sun_data)
    sun_obj.rotation_euler = (math.radians(55), 0, math.radians(35))
    bpy.context.scene.collection.objects.link(sun_obj)

    bounds = _floor_bounds_m()
    if bounds is None:
        return
    x_min, x_max, y_min, y_max, z_top = bounds
    x0, x1 = x_min + _DOWNLIGHT_INSET_M, x_max - _DOWNLIGHT_INSET_M
    y0, y1 = y_min + _DOWNLIGHT_INSET_M, y_max - _DOWNLIGHT_INSET_M
    if x1 <= x0 or y1 <= y0:
        return
    n_x = max(1, round((x1 - x0) / _DOWNLIGHT_SPACING_M))
    n_y = max(1, round((y1 - y0) / _DOWNLIGHT_SPACING_M))
    z = z_top - _DOWNLIGHT_DROP_BELOW_CEILING_M

    for i in range(n_x + 1):
        for j in range(n_y + 1):
            x = x0 if n_x == 0 else x0 + (x1 - x0) * i / n_x
            y = y0 if n_y == 0 else y0 + (y1 - y0) * j / n_y
            name = f"Downlight_{i}_{j}"
            light_data = bpy.data.lights.new(name, type="AREA")
            light_data.shape = "DISK"
            light_data.size = _DOWNLIGHT_SIZE_M
            light_data.energy = _DOWNLIGHT_ENERGY_W
            light_obj = bpy.data.objects.new(name, light_data)
            light_obj.location = (x, y, z)
            light_obj.rotation_euler = (math.pi, 0, 0)  # disk faces down (-Z)
            bpy.context.scene.collection.objects.link(light_obj)


def _configure_render(out_png_path: str, samples: int, width: int, height: int) -> None:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    scene.cycles.use_denoising = True
    try:
        scene.cycles.denoiser = "OPENIMAGEDENOISE"
    except TypeError:
        pass  # older/newer Blender: denoiser choice differs, default is fine
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.filepath = out_png_path
    scene.render.image_settings.file_format = "PNG"


def main() -> None:
    args = _parse_args()
    t0 = time.time()

    _clear_scene()
    _import_glb(args.glb_path)
    if not args.show_ceiling:
        _hide_ceiling()
    _rescale_to_metres()
    _build_world(args.hdri_path)
    _place_camera(tuple(args.cam), tuple(args.target), args.orthographic, args.lens)
    _add_lights()
    _configure_render(args.out_png_path, args.samples, args.width, args.height)

    t_setup = time.time()
    bpy.ops.render.render(write_still=True)
    t_render = time.time()

    print(
        f"PLANFORGE_RENDER_TIMING setup={t_setup - t0:.2f}s "
        f"render={t_render - t_setup:.2f}s total={t_render - t0:.2f}s"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
