# PlanForge AI (v1) — Checkpoint Analysis

> Grounded in the **as-built** Clean Architecture platform (`app/`), not aspirational.
> Date: 2026-06-29. Companion to `docs/ENTERPRISE_ARCHITECTURE.md`.

## Where AI is used (read this first)

There are **two distinct AI roles** — do not conflate them:

| Role | Engine | API key? | Where in code |
|---|---|---|---|
| **Embeddings** (text → 1024-d vectors) | **BGE-M3, local, CPU** | **Never** | `app/infrastructure/embedding/bge_m3.py` |
| **Normalization** (parsed JSON → clean layout) | OpenAI / Gemini / Ollama / **None (heuristic)** | Only if you pick a cloud provider | `app/core/llm_client.py` |
| **RAG answer** (`/llm/generate`) | OpenAI / Azure / Ollama / OpenRouter / Gemini | Only if you pick a cloud provider | `app/application/use_cases/rag_generate.py` |

BGE-M3 **cannot** replace the LLM for normalization/answers — it only makes vectors. To run fully key-free: set the provider to **None** (heuristic normalization) or **Ollama**, and embeddings/retrieval stay local regardless.

---

## 1. OpenAI — exactly what we send (security audit)

All payloads are JSON-serialized and **hard-truncated to `_LLM_MAX_INPUT_CHARS = 120_000`** before transmission (`app/core/llm_client.py:17`). There are three send-modes, in increasing data exposure:

### (a) `none` / heuristic — **nothing leaves the machine**
No network call. Geometry never transmitted.

### (b) `llm_labels_only` — **labels only, no geometry** (recommended for IP safety)
Only indices + text labels are sent. **No coordinates, polygons, or wall lengths.** (`llm_client.py:244-249`)
```json
{
  "task": "improve_labels_only",
  "rooms":    [{"index": 0, "name": "RETAIL ROOM"}],
  "openings": [{"index": 0, "label": "Store Room Door"}],
  "fixtures": [{"index": 0, "label": "65\" Smart Signage"}]
}
```
The reply is a patch (`room_names` / `opening_labels` / `fixture_labels`) merged onto the **locally-computed** geometry. Geometric IP stays local.

### (c) `llm_merge` — **full geometry** (highest exposure)
The entire parsed payload — coordinates, polygons, segment lists, filename, OCR text — is sent (`llm_client.py:338-348`):
```json
{ "parsed": { "walls": [{"x1":0,"y1":0,"x2":12090,"y2":0}], "rooms":[{"name":"...","polygon":[[...]]}], "fixtures":[...], "openings":[...] },
  "file_type": "sheet", "filename": "planforge_sample.csv" }
```

### Safeguards in place
- **120k-char truncation** on every LLM call.
- **Structural gates** (`app/services/layout_llm_guard.py`) reject an LLM layout that drifts from the heuristic baseline (>15% wall/room loss, >30% bbox shrink, centroid drift) → falls back to local heuristic.
- **RAG path never sends the full document** — `PromptBuilder` (`app/application/use_cases/prompt_builder.py`) sends only `system prompt + retrieved chunks + query`.
- **Default is `none`** — no cloud call unless explicitly selected in the Studio AI panel.

**Checkpoint:** for corporate-data safety, run `llm_labels_only` (or `none`/Ollama). Avoid `llm_merge` for sensitive plans.

---

## 2. Vector DB — what we save / retrieve

PostgreSQL + **pgvector 0.8.3** (single store for relational + vectors).

**Saved** (`document_chunks` + `embeddings` tables):
- `document_chunks`: `content` (text), `chunk_index`, `token_count`, and a JSONB `metadata` blob carrying provenance — `project_id, file_id, version, sheet, floor, room, wall, fixture, source_cell, source_page, source_coords, revision, chunk_id`.
- `embeddings`: `vector(1024)` (BGE-M3), `model`, `dim`, 1:1 to a chunk. ANN index: **HNSW, cosine** (`ix_embeddings_vec`); JSONB GIN index on metadata (`ix_document_chunks_metadata`).

**Retrieved** (`app/infrastructure/vector/pgvector_repository.py`): top-k by cosine similarity, with **metadata filters** (project / room / revision / document_type) applied in the same SQL. Returns `{chunk_id, content, metadata, score, rank}`.

Verified live: 23 chunks embedded, query `"door opening"` → `Store Room Door` (score 0.648).

---

## 3. Embedding model — CPU fit

**Chosen: `BAAI/bge-m3`** (1024-d) on CPU via `sentence-transformers`. Loads once (~16s), thread-safe lazy singleton, L2-normalized output (cosine == dot).

| Model | Dim | Disk | CPU latency | Note |
|---|---|---|---|---|
| `all-MiniLM-L6-v2` | 384 | ~90 MB | <10 ms | fastest, lower accuracy |
| `bge-small-en-v1.5` | 384 | ~130 MB | ~15 ms | strong English-only |
| **`bge-m3` (current)** | **1024** | **~2.3 GB** | ~50–150 ms/sentence | multilingual, best retrieval; batches well |

Fits "perfectly" because: no GPU/API, multilingual (handles mixed CAD/vendor text), high retrieval quality, and batching amortizes CPU cost. Avoid `bge-large` (>1.5 GB, >300 ms) on CPU.

---

## 4. UI — what / where / how much

- **What:** one file per run — `sheet` (xlsx, csv, zip, pdf), `image` (png/jpg/webp/tif/bmp), `cad` (dxf).
- **Where:** Studio at `GET /studio` → `POST /api/v1/files` (multipart: `project_id`, `file_type`, `file`); stored via `IStorage` (local `uploads/` or S3) as immutable `file_version` v1.
- **How much:** **no hard size cap is enforced in code today.** Recommended caps to add: CAD 50 MB, image 15 MB (≤300 DPI), sheet 20 MB (≤10 pages). *(Honest gap — see Open Items.)*

---

## 5. Responses (actual schemas)

```
POST /api/v1/projects        -> { id, name, description }
POST /api/v1/files           -> { file_id, project_id, file_type, version_id, original_filename }
POST /api/v1/files/{id}/parse-> { file_id, parsed_document_id, normalized_document_id, layout{walls,rooms,fixtures,openings,meta} }
POST /api/v1/normalized/{id}/render -> { output_id, project_id, svg_key, dxf_key, validation_issues[] }
GET  /api/v1/outputs/{id}/svg|dxf   -> file bytes
POST /api/v1/retrieval/search-> { query, hits:[{chunk_id, content, metadata, score, rank}] }
POST /api/v1/llm/generate    -> { output_id, project_id, answer, chunk_ids[], cost_usd }
```
Every stage writes a **new version row** (immutable lineage). Long ops will return `202 + job_id` once moved to Celery.

---

## 6. Edit input → modify output

`PATCH /api/v1/normalized/{id}` (`app/application/use_cases/edit_normalized.py`): a human edit **never mutates** — it inserts a **child version** (`edit_source="human"`, `parent_version_id` set), marks the parent `superseded`, and preserves the AI original for the correction dataset. Re-render from the edited version to regenerate SVG/DXF. The raw layout JSON is editable field-by-field (labels, coordinates, polygons).

---

## 7. UI components (wireframe)

```
┌────────────────────────────────────────────── PlanForge Studio (v1) ── [AI: None ▾] ┐
│ ▼ AI Model                                                                            │
│   ( None ) ( Ollama ) ( OpenAI ) ( Gemini )     Normalize mode: [labels only ▾]       │
│   [ Save ]  [ Test Connection ]   → live result box                                   │
├───────────────────────────────────────────────────────────────────────────────────┤
│ Upload                                                                                 │
│   Input type [Sheet ▾]   Project name [____]                                           │
│   ┌─ Drag & drop file ─┐                                   [ Run pipeline ]            │
│   1·Project → 2·Upload → 3·Parse+Normalize → 4·Render      status / "Normalized by:"   │
├───────────────────────────────────────────────────────────────────────────────────┤
│ Result      [✓ fit width]                                   [ ⬇ Download DXF ]         │
│   ┌──────────── rendered SVG plan (scroll) ────────────┐                              │
└───────────────────────────────────────────────────────────────────────────────────┘
```
Implemented in `app/templates/studio_v1.html`.

---

## 8. Feedback mechanism — **enterprise loop, NOT RLHF**

`feedback` → optional `corrections` → approved → `training_examples` (chat-format JSONL export). Captures rating, comments, reason, approval status, original vs corrected layout. No reward model / PPO. Endpoints: `POST /feedback`, `POST /corrections/{id}/approve`, `GET /training-examples/export`. (`app/application/use_cases/feedback.py`)

---

## 9. Real model fine-tune?

**Dataset generation only** (in scope): approved corrections export as `{"messages":[system,user,assistant]}` JSONL — ready for **cloud SFT (`gpt-4o-mini`)** or **local QLoRA (Qwen-2.5-7B / Llama-3-8B via Unsloth/Axolotl)**. Actual training is **out of scope** for now (a future job).

---

## 10. Citations — source of truth / %

Confidence tiers defined (`app/domain/value_objects/common.py::CitationConfidence`):
- **Direct (100%)** — chunk maps to an exact `source_cell` / `source_row`.
- **Inferred (75%)** — reconstructed from OCR/bbox.
- **Synthesized (30%)** — LLM-completed where source data was missing.

RAG answers record **which `chunk_ids`** were used (`llm_requests.retrieved_chunk_ids` + `retrieval_logs`), so every generated statement is traceable to source chunks. *Honest status:* the per-element `citations` array on `generated_outputs` is wired as a field but the resolver that fills it is a Phase-6 follow-up.

---

## 11. Agentic AI (target)

Currently a **linear pipeline**, not agentic. Target loop: ParserAgent → LayoutValidator → LLM-CorrectionAgent, iterating ≤3× until structural gates pass, then return verified layout. The building blocks exist (gates, validators, provider abstraction); the orchestration loop is future work.

---

## 12. High-level architecture

```
        Studio UI (/studio)
                │ REST /api/v1
        Presentation (routers, middleware)
                │
        Application (use cases, DTOs)
                │ ports
        Domain (entities, value objects, services)
                │ adapters
 ┌──────────────┼───────────────────────────────────┐
 Infra: SQLAlchemy+UoW · pgvector · BGE-M3(CPU) · LLM strategy
        · storage(local/S3) · Redis cache · Celery · parser/render adapters
                │
        PostgreSQL+pgvector   Redis   [OpenAI/Azure/Ollama/OpenRouter/Gemini]
```

## 13. Low-level flow

```
upload → file_version(v1) → parse → parsed_document(v2) → normalize(heuristic|LLM+gates)
 → normalized_document(v3) → [human edit → v4] → chunk → BGE-M3 embed → embeddings(pgvector)
 → retrieval(embed query → cosine top-k + metadata filter) → PromptBuilder(system+ctx+query)
 → LLM → answer + audit(llm_request/response/retrieval_logs) → feedback → correction → training_example
 render: normalized → light_snap → validate → SVG + DXF → generated_output
```

## 14. Flow diagram

```mermaid
flowchart TD
  U[Upload sheet/image/cad] --> P[Parse (ezdxf/opencv/pdfplumber)]
  P --> N{Provider?}
  N -->|none| H[Heuristic normalize]
  N -->|labels_only| L[LLM labels → merge onto geometry]
  N -->|merge| M[LLM full → structural gates] -->|fail| H
  H --> V[normalized_document vN]
  L --> V
  M -->|pass| V
  V --> E[Chunk → BGE-M3 embed → pgvector]
  V --> R[Render SVG + DXF]
  E --> Q[Query → cosine top-k + filters]
  Q --> PB[PromptBuilder: system+context+query] --> G[LLM answer + audit]
  R --> UI[Studio result]
  G --> UI
```

---

## System requirements

**CPU-only (cloud LLM for normalization, BGE-M3 local):**
- Linux, 4 vCPU, **8 GB RAM** (BGE-M3 needs ~2–3 GB resident), 20 GB SSD.
- Postgres 16 + pgvector, Redis, Python 3.11.

**Self-hosted / key-free (Ollama for LLM too):**
- 8+ vCPU, 32 GB RAM, GPU (RTX 3090/4090/A10G, 24 GB VRAM) for a local 7–32B model, 100 GB NVMe.

(Embeddings run fine on CPU in either case; GPU only helps the *generative* LLM.)

---

## OpenAI cost projection

**Assumed per-file token usage** (normalization or one RAG call):
- Input ≈ **4,000 tokens** (~15k chars of parsed JSON / context)
- Output ≈ **1,500 tokens** (layout JSON / answer)

| | gpt-4o ($5 / $15 per 1M) | gpt-4o-mini ($0.15 / $0.60 per 1M) |
|---|---|---|
| **Per file** | 4000·5/1M + 1500·15/1M = **$0.0425** | 4000·0.15/1M + 1500·0.60/1M = **$0.0015** |
| 1,000 files | $42.50 | $1.50 |
| 10,000 files | $425.00 | $15.00 |
| 100,000 files | $4,250.00 | $150.00 |

**Notes:**
- `llm_labels_only` cuts input tokens sharply (only labels sent, not geometry) → real cost typically **below** these estimates.
- **Embeddings cost $0** (BGE-M3 local) at any volume.
- `none`/Ollama paths cost **$0** in API fees.
- Recommendation: **`gpt-4o-mini` + `llm_labels_only` + structural gates** → ~96% cheaper than `gpt-4o`, with local validation guarding quality.

---

## Open items (honest gaps)
- Upload size limits not yet enforced (recommended caps listed in §4).
- Per-element citation resolver not yet filling `generated_outputs.citations` (§10).
- Agentic self-correction loop not yet built (§11).
- `/embeddings` runs in the web process; Celery-enqueued variant pending.
- Real fine-tuning is dataset-only for now (§9).
