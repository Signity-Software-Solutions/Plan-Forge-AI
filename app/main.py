import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import routes_output, routes_process, routes_settings, routes_upload
from app.core.config import get_settings
from app.utils.file_handler import ensure_dirs

_APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())
    ensure_dirs()
    logger.info("%s starting (debug=%s)", settings.app_name, settings.debug)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    servers = [{"url": "/", "description": "Same origin"}]
    pub = (settings.public_api_url or "").strip().rstrip("/")
    if pub:
        servers.insert(0, {"url": pub, "description": "Configured (PUBLIC_API_URL)"})
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        servers=servers,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(routes_upload.router)
    app.include_router(routes_process.router)
    app.include_router(routes_output.router)
    app.include_router(routes_settings.router)

    static_dir = _APP_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def root():
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{settings.app_name}</title></head>
<body style="font-family:system-ui,sans-serif;padding:2rem;background:#0f172a;color:#e2e8f0;">
<h1 style="margin-top:0">{settings.app_name}</h1>
<p><a href="/studio" style="color:#38bdf8">Open Studio</a> — upload and view layouts in the browser.</p>
<p><a href="/docs" style="color:#38bdf8">API docs</a> · <a href="/health" style="color:#38bdf8">Health</a></p>
</body></html>"""
        )

    @app.get("/studio")
    def studio_page(request: Request):
        # Starlette expects (request, name, context); old (name, dict) passes dict as template name → Jinja cache TypeError.
        return templates.TemplateResponse(
            request,
            "studio.html",
            {"app_name": settings.app_name},
        )

    @app.get("/api")
    def api_summary():
        """JSON discovery for scripts (replacing the former bare `/` JSON response)."""
        return {
            "service": settings.app_name,
            "studio": "/studio",
            "docs": "/docs",
            "health": "/health",
            "upload": "POST /upload (multipart: file, type=cad|image|sheet)",
            "layout_2d": "GET /output/{project_id}/svg (after POST /generate-layout/{project_id})",
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/routes")
    def list_routes():
        """Which paths are registered — use this if POST /upload returns 404 (wrong process or stale server)."""
        rows = []
        for r in app.routes:
            path = getattr(r, "path", None)
            methods = getattr(r, "methods", None)
            if path and methods:
                rows.append(f"{','.join(sorted(methods))} {path}")
        return {"count": len(rows), "routes": sorted(rows)}

    return app


app = create_app()
