# PlanForge AI

FastAPI service that turns retail floor-plan inputs into a structured layout and
renders it in 2D and 3D.

- **Inputs:** CAD (DXF), floor-plan images, and sheets (Excel / CSV / PDF, or a ZIP of CSVs)
- **Core output:** a unified layout JSON (walls, rooms, fixtures, openings)
- **Exports:** SVG, DXF, a 3D massing model (GLB), and optional photoreal renders (PNG still / 360° GIF turntable)
- **UI:** a browser Studio at `/studio`

## How it works

```
upload → process (parse) → normalize → generate-layout → outputs
                               │
                               ├─ heuristic (default, no API keys needed)
                               └─ optional LLM: Ollama, OpenAI, Gemini, or any OpenAI-compatible API
```

- **Parse:** ezdxf (CAD), OpenCV + Tesseract OCR (images), pandas / openpyxl / pdfplumber (sheets).
- **Normalize:** builds the unified layout. For CAD and image inputs, an LLM result is checked against
  the heuristic result and rejected if it fails structural checks. Sheets use the heuristic only
  by default, so spreadsheet geometry is not rewritten by a model.
- **Generate:** validates the layout and writes SVG and `layout.json` to `outputs/{project_id}/`.

## Tech stack

- Python 3.11+, FastAPI, Uvicorn, Pydantic, Jinja2
- ezdxf, Shapely, svgwrite, OpenCV, Tesseract, pdfplumber, pandas, openpyxl
- trimesh + mapbox-earcut (3D massing), three.js (3D viewer, loaded in the browser)
- Blender / Cycles (optional, photoreal renders only)

## Quick start

Prerequisites: Python 3.11+, and Tesseract OCR for image uploads
(`sudo apt install tesseract-ocr` on Ubuntu).

```bash
git clone https://github.com/Signity-Software-Solutions/Plan-Forge-AI.git
cd Plan-Forge-AI
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # optional, every setting has a default
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open:

- **Studio:** http://127.0.0.1:8000/studio
- **API docs (Swagger):** http://127.0.0.1:8000/docs

No database is required. Project and file records are kept in memory and reset on restart;
outputs already written to `outputs/{project_id}/` can still be served after a restart.

## Studio

The browser UI at `/studio`:

- **AI Model panel:** pick the normalization provider (None / Ollama / OpenAI / Gemini).
  A provider is only selectable when its credentials are set in `.env`. The choice is kept
  in memory and resets on restart.
- **Diagram:** the 2D SVG layout.
- **DXF View:** the exported DXF.
- **3D Massing:** interactive 3D model (orbit, layer toggles, dimension labels).
- **Photoreal:** Blender-rendered **Still** or 360° **Turntable** (needs Blender, see below).

## Sample data

- `samples/planforge_sample.xlsx` and `samples/planforge_sample.csv`: a demo retail store
  (rooms, walls, fixtures, dimensions, display models, openings). Upload with `type=sheet`.
- `generate_planforge_sample.py`: regenerates the sample workbook (`python generate_planforge_sample.py`).

## API

Typical flow (`file_id` and `project_id` are UUIDs):

1. `POST /upload`: form fields `file`, `type` (`cad` | `image` | `sheet`), optional `project_name` / `project_id`
2. `POST /process/{file_id}`: parse the file
3. `POST /normalize/{file_id}`: build the unified layout (heuristic or LLM)
4. `POST /generate-layout/{project_id}`: validate and write SVG + `layout.json`
5. `GET /output/{project_id}`: layout JSON + SVG

All endpoints:

| Endpoint | Purpose |
|----------|---------|
| `GET /`, `GET /studio` | Landing page, Studio UI |
| `GET /health`, `GET /api`, `GET /routes` | Health check, API discovery, route list |
| `POST /upload`, `POST /process/{file_id}`, `POST /normalize/{file_id}` | Ingest pipeline |
| `POST /generate-layout/{project_id}` | Build outputs |
| `GET /output/{project_id}` (`/svg`, `/dxf`) | Layout JSON, raw SVG, DXF |
| `GET /output/{project_id}/glb`, `GET .../massing-meta` | 3D massing model, its dimensions and provenance |
| `POST .../render`, `GET .../render/status`, `GET .../render.png` | Photoreal still |
| `POST .../turntable`, `GET .../turntable/status`, `POST .../turntable/cancel`, `GET .../turntable.gif` | Photoreal turntable |
| `GET /api/llm-config`, `POST /api/llm-config`, `POST /api/llm-test` | Read / switch / test the LLM provider at runtime |

## Configuration

Copy `.env.example` to `.env`. All settings are optional. Never commit `.env`.
The full list with defaults is in `app/core/config.py`.

| Variable | Description |
|----------|-------------|
| `OLLAMA_HOST` / `OLLAMA_MODEL` | Ollama for normalization (both required together) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI provider (model default `gpt-4o`) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Gemini provider (model default `gemini-1.5-flash-001`) |
| `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` | Any OpenAI-compatible chat-completions API |
| `PLANFORGE_SHEET_NORMALIZE_MODE` | Sheets: `heuristic_only` (default), `llm_labels_only` (LLM fixes names/labels only), `llm_merge` (experimental: full LLM layout, accepted only if checks pass) |
| `PLANFORGE_REFERENCE_PLAN_IMAGE` | Optional PNG/JPG drawn faintly behind sheet SVGs |
| `TESSERACT_CMD` | Path to `tesseract` if it is not on `PATH` |
| `UPLOAD_DIR` / `OUTPUT_DIR` / `LOG_LEVEL` | Defaults `uploads` / `outputs` / `INFO` |
| `PUBLIC_API_URL` | Server URL shown in Swagger |
| `MESH_CEILING_HEIGHT_MM` / `MESH_WALL_THICKNESS_MM` | 3D fallbacks when the layout gives none (defaults `3000` / `100`) |
| `BLENDER_BIN` | Blender binary for photoreal renders (default `.tools/blender/blender` if present) |
| `PHOTOREAL_SAMPLES` / `_WIDTH` / `_HEIGHT` | Still render quality (defaults `128` / `1280` / `720`) |
| `PHOTOREAL_TURNTABLE_FRAMES` / `_SAMPLES` / `_WIDTH` / `_HEIGHT` | Turntable settings (defaults `24` / `48` / `960` / `540`) |

## 3D massing

`app/services/mesh_builder.py` builds the GLB from the same unified layout used for SVG and DXF:
walls are extruded to ceiling height, openings are cut as jambs / lintels / sills, and fixtures are
built from per-type recipes in `app/services/fixture_types.py` (with a few CC0 assets in `app/assets/`).

- **Never stored:** the GLB is rebuilt on every request (fast and deterministic, with an `ETag`) and never written to disk.
- **Heights:** the 2D layout has no ceiling height or wall thickness. Set them in `layout.meta`
  (`ceiling_height_mm`, `wall_thickness_mm`); otherwise defaults are used. The response reports which
  (`provenance.z_source` = `user_supplied` | `assumed_default`, also in the `X-PlanForge-Z-Source` header),
  and the viewer shows a "ceiling height assumed · not for construction" badge.
- **Wall-mounted fixtures:** fixtures sit on the floor unless an elevation is declared in
  `layout.meta.fixture_elevations_mm`, matched by exact label, then kind, then `"*"`:

  ```json
  "meta": {
    "ceiling_height_mm": 3200,
    "fixture_elevations_mm": { "display": 900, "*": 0 }
  }
  ```

- **Triangulation uses `engine="earcut"`.** Do not install the `triangle` package (not licensed for commercial use).

## Photoreal render (optional)

Renders the layout with Blender / Cycles on the server. The rest of the app works without it;
without Blender, these endpoints return 503.

```bash
bash scripts/setup_blender.sh    # portable Blender under .tools/ (~1.3 GB, gitignored, no root needed)
```

- Blender runs as a separate subprocess (`app/blender/render_scene.py`) and is never imported into the app (GPL).
- **Still:** one PNG per project; pick a viewpoint with `?viewpoint_id=` (e.g. `PLAN`, `A`–`D`).
  About 40–65 s on an 8-thread CPU without a GPU.
- **Turntable:** 24 frames around the model, stitched into a looping GIF. About 5 minutes on the same CPU.
- Render jobs are tracked in memory (single worker); a restart mid-render loses the job.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/run_pipeline.sh` | Runs the full pipeline against a running API. Set `PLANFORGE_API_BASE_URL` and `PLANFORGE_INPUT_FILE` in `.env`; optional `PLANFORGE_INPUT_TYPE`, `PLANFORGE_PROJECT_NAME`, `PLANFORGE_PIPELINE_OUTPUT_DIR` (default `./pipeline_output`) |
| `scripts/test_cad_to_2d.sh <image>` | Smoke test for an image upload (needs `curl` and `jq`) |
| `scripts/test_remote_ollama.py` | Checks an Ollama server works with the normalize flow |
| `scripts/setup_blender.sh` | Installs portable Blender for photoreal renders |

## Tests

```bash
pytest
```

## Deployment

- **Docker:** `docker build -t planforge-ai .` then `docker run -p 8000:8000 --env-file .env planforge-ai`.
  The image does not include Tesseract or Blender, so image OCR and photoreal renders are unavailable in it.
- **PaaS (Procfile / Railpack):** starts `uvicorn app.main:app` on `$PORT`; Railpack also installs Tesseract.

## Project structure

```
app/
  main.py          FastAPI app, routers, Studio routes
  state.py         in-memory registry (files, projects, runtime LLM choice)
  api/             upload, process, output, LLM settings routes
  core/            settings, LLM client
  models/          Pydantic schema (UnifiedLayout)
  services/        parsers, normalization, SVG/DXF/3D builders, photoreal orchestration
  blender/         Blender render script (runs in a subprocess)
  assets/          CC0 3D models and textures
  static/          Studio CSS/JS, 3D viewer, DXF viewer, HDRI
  templates/       Studio UI (Jinja2)
  utils/           file handling, validation, disk fallbacks
scripts/           pipeline, smoke tests, Blender setup
samples/           demo workbook + CSV
tests/             pytest suite
ARCH.md            detailed architecture reference
```

## License

No license file yet. Add one before others reuse this code.
