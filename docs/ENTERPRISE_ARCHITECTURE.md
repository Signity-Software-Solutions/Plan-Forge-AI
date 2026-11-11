# PlanForge AI — Enterprise Platform (v1)

Clean Architecture rebuild of the backend. The legacy MVP (`app.main:app`,
`app/services`, `app/api`, `app/state.py`) keeps running unchanged; the new
platform lives under the Onion layers in `app/` and is served from
`app.presentation.app:app`. The full design is in
`/home/signity/.claude/plans/you-are-acting-as-lazy-catmull.md`.

## Layers

```
app/
  core/            config (nested) · logging · exceptions · security · DI
  domain/          value_objects · interfaces (ports) · repositories · services
  application/     dto · use_cases (one per operation) · prompt builder · retrieval
  infrastructure/  database (ORM, UoW, Alembic) · repositories · vector(pgvector)
                   · llm(strategy) · embedding(BGE-M3) · storage · cache
                   · parsing/rendering adapters (wrap legacy code) · tasks(Celery)
  presentation/    routers · schemas · middleware · api/v1
```

Dependencies point inward only. Infrastructure adapters implement domain
**ports** (`app/domain/interfaces/ports.py`); the composition root
(`app/core/dependency_injection.py`) is the single place that binds them.

## What is reused from the MVP (behind adapters)

- Parsers: `app/services/{cad,image,sheet}_parser.py` → `infrastructure/parsing/adapters.py`
- Geometry/DXF: `geometry_engine.py`, `dxf_exporter.py` → `infrastructure/rendering/adapters.py`
- Normalization + gates: `core/llm_client.py`, `layout_llm_guard.py` (called from the parse use case)
- `UnifiedLayout` schema → re-exported as a domain value object

## Run it locally

```bash
# 1. Infra (Postgres+pgvector, Redis)
docker compose up -d

# 2. Deps (torch/sentence-transformers are large; needed only for the worker)
pip install -r requirements.txt

# 3. Migrate (creates pgvector + all 18 tables + ANN/GIN indexes)
alembic upgrade head

# 4. Web API (no model loaded in this process)
uvicorn app.presentation.app:app --reload   # -> http://127.0.0.1:8000/api/v1/health

# 5. Worker (loads BGE-M3 once on startup)
celery -A worker.celery_app worker --loglevel=info
```

## End-to-end flow (API)

1. `POST /api/v1/projects` → project_id
2. `POST /api/v1/files` (multipart: project_id, file_type, file) → file_id (+ v1)
3. `POST /api/v1/files/{file_id}/parse` → parsed (v2) + normalized (v3)
4. `PATCH /api/v1/normalized/{id}` → human-edited child version (v4) — original preserved
5. `POST /api/v1/embeddings/{normalized_id}` → chunk + BGE-M3 embed → pgvector
6. `POST /api/v1/retrieval/search` → filtered top-k chunks
7. `POST /api/v1/llm/generate` → RAG answer + full audit (request/response/cost/citations)
8. `POST /api/v1/normalized/{id}/render` → SVG + DXF stored, validation issues returned
9. `POST /api/v1/feedback` (+ optional correction) → `POST /corrections/{id}/approve` → training example
10. `GET /api/v1/training-examples/export` → chat-format JSONL

## Key invariants

- **Embeddings are local only** (BGE-M3, CPU). No external embedding API — there
  is deliberately no embedding method on `ILLMProvider`.
- **Nothing is overwritten.** Every stage appends a version row with
  `parent_version_id`; edits supersede, never mutate.
- **LLMs never see the raw document** — only system prompt + retrieved chunks +
  query, assembled by `PromptBuilder`.
- **Every AI call is audited** (provider, model, tokens, cost, latency, chunk ids).

## Studio UI (v1)

The Studio UI is served by the v1 app itself: `GET /studio` (and `/` redirects to
it). Template `app/templates/studio_v1.html`, styled by the existing
`app/static/css/studio.css`. Its JS drives the v1 flow end to end:
create project → upload (`/api/v1/files`) → parse+normalize
(`/api/v1/files/{id}/parse`) → render (`/api/v1/normalized/{id}/render`) →
SVG/DXF (`/api/v1/outputs/{id}/svg|dxf`). The AI Model panel uses
`/api/v1/llm/{config,test}`; the selected provider/mode is held in
`app/infrastructure/llm/runtime_config.py` and passed into the parse step as
`llm_override` (resets on restart).

## Status by phase

- ✅ Phase 2 — DB + Clean Architecture scaffolding (config, DI, ORM, Alembic, UoW, repos, routers)
- ✅ Phase 3 — BGE-M3 embedding singleton + pgvector repo + chunking + Celery worker
- ✅ Phase 4 — retrieval service + prompt builder
- ✅ Phase 5 — LLM provider strategy (OpenAI/Azure/Ollama/OpenRouter/Gemini) + cost/audit
- ✅ Phase 6 — feedback / corrections / training-example export / audit / citations field
- ⏳ Phase 7 — tests, perf tuning, OTel, cutover of Studio UI to `/api/v1`

## Notes / follow-ups

- Prompt **versioning** table exists; wiring an active-prompt lookup into
  `RagGenerateUseCase` is a small follow-up.
- IVFFLAT vs HNSW is config-driven (`VECTOR__INDEX_KIND`, default `hnsw`).
- Auth is stubbed to a single service user; the `current_user` dependency
  signature is final so JWT/RBAC drops in without touching routers.
