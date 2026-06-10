import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.routes import (
    agent,
    analysis,
    anomalies,
    updater,
    background,
    chat,
    dashboard,
    imports,
    insights,
    knowledge,
    llm,
    maintenance,
    operator_events,
    operator_journal,
    powder,
    profiles,
    quality,
    realtime,
    sessions,
    uploads,
    web,
)
from core.config.settings import get_settings
from core.logging.config import RequestIDMiddleware, configure_logging
from core.preflight import run_preflight
from core.versioning.version import APP_VERSION
from storage.db.init_db import create_all

logger = logging.getLogger(__name__)

# Strong references so background tasks aren't GC-collected mid-run.
_BG_TASKS: set[asyncio.Task] = set()


async def _startup_import(raw_logs_path: str) -> None:
    """On startup: scan the raw-logs folder and import any unprocessed sessions.

    The watcher only reacts to NEW files arriving while it's running.
    This task makes sure sessions that were on disk before the containers
    started (or while they were down) are picked up automatically.

    Idempotent: already-imported sessions are detected by group_id and skipped.
    Runs 15 seconds after startup to let the database finish initialising.
    """
    await asyncio.sleep(15)
    path = Path(raw_logs_path)
    if not path.exists() or not path.is_dir():
        logger.warning("startup_import: raw-logs path not found: %s", path)
        return

    logger.info("startup_import: scanning %s for unimported sessions …", path)
    try:
        # Lazy imports — avoid loading heavy ML deps at module level.
        from domain.services.session_import import import_new_sessions
        from storage.db.session import SessionLocal
        from storage.repositories.runtime import RuntimeRepository

        with SessionLocal() as db:
            # save_session_payload commits each row; no extra commit needed.
            stats = import_new_sessions(path, RuntimeRepository(db))

        if not stats["found"]:
            logger.info("startup_import: no log groups found in %s", path)
            return
        logger.info("startup_import: done — %d new session(s) imported, %d already existed",
                    stats["imported"], stats["found"] - stats["imported"])
    except Exception:
        logger.exception("startup_import: failed (non-fatal)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.app_env == "local":
        create_all()
    # LLM endpoint discovery moved out of Settings construction (it did blocking
    # network I/O at import time). Run it here, once, on the interactive path.
    from core.config.settings import discover_and_apply_llm
    discover_and_apply_llm(settings)
    report = run_preflight(settings, component="api")
    for warn in report.warnings:
        logging.getLogger("preflight").warning(warn)

    # Kick off background import of existing log files.
    task = asyncio.create_task(_startup_import(settings.raw_logs_container_path))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)

    yield


settings = get_settings()
configure_logging(settings.log_level)

app = FastAPI(
    title="Printer Log Analytics",
    version=APP_VERSION,
    description="Extensible industrial log analytics platform for metal 3D printers.",
    lifespan=lifespan,
)

app.add_middleware(RequestIDMiddleware)

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
# allow_credentials=True is incompatible with a wildcard origin (the browser
# rejects it, and mirroring arbitrary origins enables CSRF). Only send
# credentials when an explicit allow-list is configured; otherwise fall back to
# a wildcard origin with credentials disabled.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=bool(origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": APP_VERSION, "llm_provider": settings.llm_provider}


app.include_router(sessions.router)
app.include_router(imports.router)
app.include_router(anomalies.router)
app.include_router(profiles.router)
app.include_router(operator_events.router)
app.include_router(operator_journal.router)
app.include_router(quality.router)
app.include_router(background.router)
app.include_router(insights.router)
app.include_router(knowledge.router)
app.include_router(agent.router)
app.include_router(llm.router)
app.include_router(llm.reports_router)
app.include_router(chat.router)
app.include_router(web.router)
app.include_router(dashboard.router)
app.include_router(realtime.router)
app.include_router(maintenance.router)
app.include_router(powder.router)
app.include_router(analysis.router)
app.include_router(updater.router)
app.include_router(uploads.router)


@app.get("/alarm-demo", response_class=HTMLResponse)
async def alarm_demo():
    html = (Path(__file__).parent.parent / "alarm_demo.html").read_text(encoding="utf-8")
    return html
