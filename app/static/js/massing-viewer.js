// 3D massing viewer (Tier A+) — loads the GLB from /output/{id}/glb and renders
// a doll's-house axonometric of the space.
//
// Imported on demand by studio.html the first time the 3D tab is opened, so the
// three.js payload never reaches users who only look at the 2D plan. The static
// imports below still code-split, because this whole module is only fetched at
// that point.
//
// "three" and "three/addons/" resolve through the import map in base.html.
// Without it these imports fail outright — three.js addons ship bare specifiers.
//
// Coordinate note: mesh_builder builds in layout mm, Z-up, and puts the
// Z-up -> Y-up rotation on the GLB root. Geometry therefore arrives correctly
// oriented, but dimensions and viewpoints from /massing-meta are still in layout
// coordinates, so both apply the same mapping: (x, y, z) -> (x, z, -y).

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/renderers/CSS2DRenderer.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { RGBELoader } from "three/addons/loaders/RGBELoader.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { SAOPass } from "three/addons/postprocessing/SAOPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";

// Scene group names — must match GROUPS in app/services/mesh_builder.py.
const GROUPS = ["walls", "fixtures", "ceiling", "floor"];
// Retail designers read a plan with the ceiling off; an extruded slab at ceiling
// level hides the entire interior. Off by default.
const DEFAULT_HIDDEN = ["ceiling"];

// Presentation ground. Proposals put the model on a white page; a dark viewport
// reads as a CAD tool no matter how good the geometry is.
const BACKGROUND = 0xf4f4f6;

// Layout Z-up -> viewer Y-up.
const toViewer = (x, y, z) => [x, z || 0, -y];

let viewer = null;

// WebGL is not guaranteed: Chrome removed the SwiftShader CPU fallback, so on
// an IT-locked or VDI workstation context creation fails outright rather than
// degrading. Detect and fall back to the 2D view instead of a black canvas.
function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(c.getContext("webgl2") || c.getContext("webgl"));
  } catch (e) {
    return false;
  }
}

class MassingViewer {
  constructor(host) {
    this.host = host;
    this.groups = new Map();
    this.labels = [];
    this.viewpoints = [];
    // Off by default. Every fixture carrying its dimensions on screen turns the
    // model into a measured drawing, which is not what a design proposal's plan
    // view is for; the numbers are still one setLabelsVisible(true) away, and
    // the DXF and SVG routes exist precisely for the dimensioned read.
    this.showLabels = false;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(BACKGROUND);

    const aspect = 1;
    this.perspective = new THREE.PerspectiveCamera(45, aspect, 10, 500000);
    // Orthographic is what makes a true axonometric; frustum is set in frame().
    this.ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, -100000, 500000);
    this.camera = this.perspective;

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    // The environment, the key light and tone mapping all add exposure; left at
    // 1.0 the dark cabinetry washes out to white and the band scheme stops
    // reading, which is the whole point of typing fixtures.
    this.renderer.toneMappingExposure = 0.68;
    this.host.appendChild(this.renderer.domElement);

    // Image-based lighting. Start from a procedural room so the first frame
    // renders immediately without waiting on a network round trip; it already
    // gives flat colours soft gradients and directionality, which is most of
    // what separates a rendered-looking massing model from flat rectangles.
    this._pmrem = new THREE.PMREMGenerator(this.renderer);
    this.scene.environment = this._pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    this.scene.environmentIntensity = 0.50;
    // Then swap in a real CC0 HDRI (app/static/env/manifest.json) for actual
    // reflections and a lighting colour drawn from a genuine indoor retail
    // space, instead of the procedural room's synthetic gradient. Best-effort:
    // network failure or a blocked fetch just leaves the procedural room in
    // place, never a black scene — same fail-safe stance as _selectRenderPath.
    this._loadHdriEnvironment();

    this.labelRenderer = new CSS2DRenderer();
    this.labelRenderer.domElement.style.position = "absolute";
    this.labelRenderer.domElement.style.top = "0";
    this.labelRenderer.domElement.style.left = "0";
    this.labelRenderer.domElement.style.pointerEvents = "none";
    this.host.appendChild(this.labelRenderer.domElement);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;

    // A key light on top of the environment, for a readable sun direction.
    const key = new THREE.DirectionalLight(0xffffff, 1.15);
    key.position.set(1, 2.2, 1.4);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    this.key = key;
    this.scene.add(key);
    this.scene.add(key.target);
    // No ambient fill: the environment already supplies it, and doubling up is
    // what flattens the contrast between bands.

    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.labelRoot = new THREE.Group();
    this.scene.add(this.labelRoot);
    this.leaderRoot = new THREE.Group();
    this.scene.add(this.leaderRoot);

    this._buildComposer();

    // Click-to-SKU. A raycast per click is cheap even at a few hundred
    // fixtures; nothing here runs per frame.
    this._raycaster = new THREE.Raycaster();
    this._pointer = new THREE.Vector2();
    this._pickCallback = null;
    this._pickHighlight = null;
    this._onClick = (ev) => this._handleClick(ev);
    this.renderer.domElement.addEventListener("click", this._onClick);

    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);

    this._animate = this._animate.bind(this);
    this.renderer.setAnimationLoop(this._animate);
  }

  /**
   * Fetch the real HDRI and, if it loads, replace the procedural room as the
   * scene's environment map. Never throws — a missing file, an offline
   * viewer, or a slow network all just keep the RoomEnvironment already in
   * place, which is itself a real (if flatter) environment, not a placeholder.
   */
  _loadHdriEnvironment() {
    new RGBELoader().load(
      "/static/hdri/phone_shop_1k.hdr",
      (hdrTexture) => {
        // Guard against dispose() already having run while this fetch was in
        // flight (the viewer is a long-lived singleton in this app, but a
        // future caller that tears it down mid-load shouldn't crash).
        if (this._pmrem) {
          this.scene.environment = this._pmrem.fromEquirectangular(hdrTexture).texture;
          this._pmrem.dispose();
          this._pmrem = null;
        }
        hdrTexture.dispose();
      },
      undefined,
      (err) => {
        console.warn("HDRI environment failed to load; keeping the procedural room.", err);
        if (this._pmrem) {
          this._pmrem.dispose();
          this._pmrem = null;
        }
      }
    );
  }

  /** Register a callback fired with fixture info when a mesh is clicked, or
   * with null when the click hit empty space (clears any selection). */
  onPick(callback) {
    this._pickCallback = callback;
  }

  _handleClick(ev) {
    if (!this._pickCallback) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    this._pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    this._pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    this._raycaster.setFromCamera(this._pointer, this.camera);

    const hits = this._raycaster.intersectObjects(this.root.children, true);
    const hit = hits.find((h) => h.object.isMesh);
    this._setHighlight(hit ? hit.object : null);
    this._pickCallback(hit ? this.describeMesh(hit.object) : null, hit ? hit.object : null);
  }

  _setHighlight(mesh) {
    if (this._pickHighlight) {
      this.root.remove(this._pickHighlight);
      this._pickHighlight.geometry.dispose();
      this._pickHighlight.material.dispose();
      this._pickHighlight = null;
    }
    if (!mesh) return;
    const box = new THREE.BoxHelper(mesh, 0xdc2626);
    box.raycast = () => {}; // never itself pickable
    this.root.add(box);
    this._pickHighlight = box;
  }

  clearPick() {
    this._setHighlight(null);
  }

  _buildComposer() {
    // Ambient occlusion is what makes boxes read as solid volumes rather than
    // floating rectangles — the single biggest perceived-quality gain here.
    this.composer = new EffectComposer(this.renderer);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.composer.addPass(this.renderPass);

    this.sao = new SAOPass(this.scene, this.camera);
    this.sao.params.saoBias = 0.5;
    this.sao.params.saoIntensity = 0.08;
    this.sao.params.saoScale = 400;
    this.sao.params.saoKernelRadius = 24;
    this.sao.params.saoBlur = true;
    this.composer.addPass(this.sao);

    this.composer.addPass(new OutputPass());

    // Ambient occlusion needs depth and float render targets. Software WebGL
    // (and some locked-down drivers) accept the passes and then return a black
    // frame, so the composer is proven before it is trusted - see
    // _selectRenderPath. Until then, render directly.
    this.usePostprocessing = false;
    this.pathSelected = false;
  }

  /**
   * Mean luminance of a patch at the centre of the last rendered frame.
   *
   * Absolute brightness cannot decide whether a GPU feature failed: against a
   * light background a completely black model still reads bright. So this
   * returns a number to *compare*, and _selectRenderPath compares each optional
   * feature against a known-good direct render.
   */
  _probeLuma() {
    const gl = this.renderer.getContext();
    const w = this.renderer.domElement.width;
    const h = this.renderer.domElement.height;
    if (!w || !h) return null;
    const n = 96;
    const px = new Uint8Array(4 * n * n);
    gl.readPixels(
      Math.max(0, Math.floor((w - n) / 2)),
      Math.max(0, Math.floor((h - n) / 2)),
      n, n, gl.RGBA, gl.UNSIGNED_BYTE, px
    );
    let sum = 0;
    for (let i = 0; i < px.length; i += 4) {
      sum += 0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2];
    }
    return sum / (n * n);
  }

  /**
   * Decide, once, which rendering features this GPU can actually deliver.
   *
   * Ambient occlusion and shadow mapping both accept their configuration,
   * report success, and then return a black or blank frame on software WebGL
   * and some locked-down drivers. Nothing throws, so the only reliable test is
   * to render and look — and to compare against a baseline rather than against
   * an absolute, since the correct answer depends on the background.
   *
   * Degrades one feature at a time and always fails safe: a flat but correct
   * model beats a black rectangle.
   */
  _selectRenderPath() {
    if (this.pathSelected) return;
    this.pathSelected = true;
    // A feature that darkens the centre of the frame by more than this against
    // a plain render is not doing subtle occlusion; it has failed.
    const FLOOR = 0.55;

    try {
      // Baseline: no shadows, no postprocessing.
      const hadShadows = this.renderer.shadowMap.enabled;
      this._setShadows(false);
      this.renderer.render(this.scene, this.camera);
      const baseline = this._probeLuma();
      if (baseline === null) return;

      // 1. shadows
      if (hadShadows) {
        this._setShadows(true);
        this.renderer.render(this.scene, this.camera);
        if (this._probeLuma() < baseline * FLOOR) {
          console.warn("[massing] shadow mapping blackened the frame; disabling shadows");
          this._setShadows(false);
          this.renderer.render(this.scene, this.camera);
        }
      }

      // 2. ambient occlusion
      this.composer.render();
      this.usePostprocessing = this._probeLuma() >= baseline * FLOOR;
      if (!this.usePostprocessing) {
        console.warn(
          "[massing] ambient occlusion blackened the frame; rendering directly"
        );
      }
    } catch (e) {
      console.warn("[massing] falling back to plain rendering:", e);
      this.usePostprocessing = false;
      this._setShadows(false);
    }
  }

  _setShadows(on) {
    this.renderer.shadowMap.enabled = on;
    if (this.key) this.key.castShadow = on;
    this.root.traverse((o) => {
      if (!o.isMesh) return;
      const flat = o.name.startsWith("floor") || o.name.startsWith("ceiling");
      o.castShadow = on && !flat;
      if (o.material) o.material.needsUpdate = true;
    });
  }

  _animate() {
    this.controls.update();
    if (this.usePostprocessing) {
      this.composer.render();
    } else {
      this.renderer.render(this.scene, this.camera);
    }
    this.labelRenderer.render(this.scene, this.camera);
  }

  _useCamera(camera) {
    if (this.camera === camera) return;
    this.camera = camera;
    this.controls.object = camera;
    this.renderPass.camera = camera;
    this.sao.camera = camera;
    this.controls.update();
  }

  resize() {
    const w = this.host.clientWidth || 1;
    const h = this.host.clientHeight || 1;
    this.perspective.aspect = w / h;
    this.perspective.updateProjectionMatrix();
    this._updateOrthoFrustum(w / h);
    this.renderer.setSize(w, h);
    this.composer.setSize(w, h);
    this.labelRenderer.setSize(w, h);
    if (this.leaderMaterial) this.leaderMaterial.resolution.set(w, h);
  }

  _updateOrthoFrustum(aspect) {
    const half = this._orthoHalfHeight || 5000;
    this.ortho.left = -half * aspect;
    this.ortho.right = half * aspect;
    this.ortho.top = half;
    this.ortho.bottom = -half;
    this.ortho.updateProjectionMatrix();
  }

  clear() {
    this._pickHighlight = null; // root.clear() below removes it; don't double-dispose
    this.root.clear();
    this.labelRoot.clear();
    this.leaderRoot.clear();
    this.groups.clear();
    this.labels = [];
    this.viewpoints = [];
  }

  async loadGlb(url) {
    const loader = new GLTFLoader();
    const gltf = await loader.loadAsync(url);
    this.root.add(gltf.scene);

    // Index the named groups so visibility can be toggled without touching the
    // backend. Names come straight from the GLB node graph.
    gltf.scene.traverse((obj) => {
      if (GROUPS.includes(obj.name)) this.groups.set(obj.name, obj);
    });
    for (const name of DEFAULT_HIDDEN) {
      const g = this.groups.get(name);
      if (g) g.visible = false;
    }
    gltf.scene.traverse((o) => {
      if (!o.isMesh) return;
      // Floors and ceilings only receive. Letting a floor plate cast onto itself
      // is pure artefact, and the shell casting inward darkens the whole room.
      const flat = o.name.startsWith("floor") || o.name.startsWith("ceiling");
      o.castShadow = !flat;
      o.receiveShadow = true;
      this._addEdgeOutline(o);
    });
    this._fitShadowCamera();
    this.frame();
    return gltf;
  }

  /**
   * Crisp line-work along each mesh's hard edges.
   *
   * The plan called for OutlinePass, but that is a postprocessing pass in the
   * same family as SAO and shadow mapping — both of which returned a silently
   * black frame under software WebGL earlier in this project. EdgesGeometry is
   * plain geometry rendered directly: no render-target dependency, works
   * wherever the base mesh renders, and gives the crisp architectural line-work
   * a boxy massing model actually wants, closer to a drawn plan than a glow.
   */
  _addEdgeOutline(mesh) {
    if (!this._edgeMaterial) {
      this._edgeMaterial = new THREE.LineBasicMaterial({
        color: 0x2a2a2a,
        transparent: true,
        opacity: 0.35,
      });
    }
    // 25 degrees keeps a box's true edges crisp without drawing a line down
    // every near-coplanar triangle earcut happens to have split a face into.
    const edges = new THREE.EdgesGeometry(mesh.geometry, 25);
    const lines = new THREE.LineSegments(edges, this._edgeMaterial);
    lines.raycast = () => {}; // decorative only — never steals a fixture pick
    mesh.add(lines);
  }

  /**
   * Aim the shadow camera at the model.
   *
   * A directional light's shadow frustum defaults to a few units across; at
   * building scale in millimetres nothing lands inside it and no shadow appears
   * at all — which looks like shadows being unsupported rather than misconfigured.
   */
  _fitShadowCamera() {
    if (!this.key) return;
    const box = new THREE.Box3().setFromObject(this.root);
    if (box.isEmpty()) return;
    const centre = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z) || 1000;

    this.key.target.position.copy(centre);
    this.key.position.set(
      centre.x + radius * 0.6,
      centre.y + radius * 1.6,
      centre.z + radius * 0.8
    );
    // Shadow bias is expressed in world units, so a value tuned for a unit-scale
    // scene is meaningless at building scale in millimetres — every surface
    // self-shadows and the model renders as a silhouette. normalBias scales with
    // the scene, which is what makes it the right control here.
    this.key.shadow.normalBias = radius * 0.004;
    this.key.shadow.bias = -radius * 1e-6;
    this.key.target.updateMatrixWorld();

    const cam = this.key.shadow.camera;
    cam.left = -radius * 0.9;
    cam.right = radius * 0.9;
    cam.top = radius * 0.9;
    cam.bottom = -radius * 0.9;
    cam.near = radius * 0.05;
    cam.far = radius * 5;
    cam.updateProjectionMatrix();
  }

  setGroupVisible(name, visible) {
    const g = this.groups.get(name);
    if (g) g.visible = visible;
  }

  /**
   * Per-mesh metadata written by mesh_builder. trimesh emits it as glTF
   * *primitive* extras, so GLTFLoader surfaces it on geometry.userData rather
   * than object.userData — worth knowing before hunting for it.
   */
  describeMesh(mesh) {
    if (!mesh) return null;
    return { ...(mesh.geometry?.userData || {}), ...(mesh.userData || {}) };
  }

  addDimensionLabels(dimensions) {
    const positions = [];
    for (const d of dimensions) {
      const el = document.createElement("div");
      el.className = "massing-label massing-label--" + (d.kind || "generic");
      el.textContent = d.label;

      const mx = (d.x1 + d.x2) / 2;
      const my = (d.y1 + d.y2) / 2;
      const mz = d.z || 0;

      // nx/ny is the outward plan normal; offsetting the callout along it is
      // what turns a floating number into a dimension line.
      const off = d.offset_mm || 450;
      const ox = mx + (d.nx || 0) * off;
      const oy = my + (d.ny || 0) * off;

      const obj = new CSS2DObject(el);
      obj.position.set(...toViewer(ox, oy, mz));
      this.labelRoot.add(obj);
      this.labels.push(obj);

      if (d.x1 !== d.x2 || d.y1 !== d.y2) {
        positions.push(...toViewer(d.x1, d.y1, mz), ...toViewer(ox, oy, mz));
        positions.push(...toViewer(d.x2, d.y2, mz), ...toViewer(ox, oy, mz));
      }
    }

    if (positions.length) {
      const geom = new LineSegmentsGeometry();
      geom.setPositions(positions);
      this.leaderMaterial = new LineMaterial({
        color: 0x54627a,
        linewidth: 1.2,
        transparent: true,
        opacity: 0.55,
      });
      this.leaderMaterial.resolution.set(
        this.host.clientWidth || 1,
        this.host.clientHeight || 1
      );
      this.leaderRoot.add(new Line2(geom, this.leaderMaterial));
    }
    this.setLabelsVisible(this.showLabels);
  }

  setLabelsVisible(visible) {
    this.showLabels = visible;
    this.labelRoot.visible = visible;
    this.leaderRoot.visible = visible;
  }

  setViewpoints(viewpoints) {
    this.viewpoints = viewpoints || [];
  }

  /** Move to a named viewpoint from /massing-meta. Falls back to frame(). */
  applyViewpoint(id) {
    const vp = this.viewpoints.find((v) => v.id === id);
    if (!vp) {
      this.frame();
      return false;
    }
    const [tx, ty, tz] = toViewer(...vp.target);
    const [px, py, pz] = toViewer(...vp.position);

    if (vp.orthographic) {
      const box = new THREE.Box3().setFromObject(this.root);
      const size = box.getSize(new THREE.Vector3());
      this._orthoHalfHeight = Math.max(size.x, size.y, size.z) * 0.62 || 5000;
      this._updateOrthoFrustum(
        (this.host.clientWidth || 1) / (this.host.clientHeight || 1)
      );
      this._useCamera(this.ortho);
    } else {
      this._useCamera(this.perspective);
    }

    this.camera.position.set(px, py, pz);
    this.controls.target.set(tx, ty, tz);
    this.camera.updateProjectionMatrix();
    this.controls.update();

    for (const name of vp.hide_groups || []) this.setGroupVisible(name, false);
    for (const name of vp.show_groups || []) this.setGroupVisible(name, true);
    // Keep the layer checkboxes honest -- a viewpoint forcing a group's
    // visibility should not leave its toggle showing the opposite state.
    const ceilingCb = document.getElementById("massing-ceiling");
    if (ceilingCb) {
      if ((vp.hide_groups || []).includes("ceiling")) ceilingCb.checked = false;
      if ((vp.show_groups || []).includes("ceiling")) ceilingCb.checked = true;
    }
    return true;
  }

  frame() {
    const box = new THREE.Box3().setFromObject(this.root);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z) || 1000;

    this.controls.target.copy(centre);
    this.camera.position.set(
      centre.x + radius * 1.0,
      centre.y + radius * 0.8,
      centre.z + radius * 1.0
    );
    if (this.camera.isPerspectiveCamera) {
      this.camera.near = radius / 500;
      this.camera.far = radius * 60;
    } else {
      this._orthoHalfHeight = radius * 0.62;
      this._updateOrthoFrustum(
        (this.host.clientWidth || 1) / (this.host.clientHeight || 1)
      );
    }
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  screenshot(filename) {
    if (this.usePostprocessing) this.composer.render();
    else this.renderer.render(this.scene, this.camera);
    this.renderer.domElement.toBlob((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  dispose() {
    window.removeEventListener("resize", this._onResize);
    this.renderer.domElement.removeEventListener("click", this._onClick);
    this.renderer.setAnimationLoop(null);
    if (this._pmrem) {
      this._pmrem.dispose();
      this._pmrem = null;
    }
    this.renderer.dispose();
  }
}

export async function mount(hostEl, projectId, ui) {
  if (!webglAvailable()) {
    throw new Error(
      "WebGL is unavailable in this browser. Use the Diagram or DXF view instead."
    );
  }
  if (!viewer) {
    viewer = new MassingViewer(hostEl);
  }
  viewer.clear();
  viewer.resize();

  // Both endpoints regenerate from the layout; nothing is read from storage.
  const [glbUrl, metaRes] = [
    `/output/${projectId}/glb`,
    await fetch(`/output/${projectId}/massing-meta`),
  ];
  if (!metaRes.ok) {
    const body = await metaRes.json().catch(() => ({}));
    throw new Error(body.detail || `massing-meta failed (${metaRes.status})`);
  }
  const meta = await metaRes.json();

  await viewer.loadGlb(glbUrl);
  // Geometry has to be present for the black-frame probe to mean anything.
  viewer._selectRenderPath();
  viewer.addDimensionLabels(meta.dimensions || []);
  viewer.setViewpoints(meta.viewpoints || []);
  viewer.applyViewpoint("PLAN");

  if (ui && typeof ui.onLoaded === "function") ui.onLoaded(meta, viewer);
  return viewer;
}

export { webglAvailable };

export function getViewer() {
  return viewer;
}
