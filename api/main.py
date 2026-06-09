import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
        from domain.services.ingestion import IngestionService
        from domain.services.session_grouping import group_files_into_sessions
        from domain.services.session_overview import build_group_overview
        from profiles.m350.profile import build_registry, get_profile
        from storage.db.session import SessionLocal
        from storage.repositories.runtime import RuntimeRepository

        registry = build_registry()
        profile = get_profile()
        result = IngestionService(registry, profile).parse(path)
        groups = group_files_into_sessions(result.files)

        if not groups:
            logger.info("startup_import: no log groups found in %s", path)
            return

        logger.info("startup_import: found %d session group(s)", len(groups))

        with SessionLocal() as db:
            repo = RuntimeRepository(db)
            existing = {sid for sid, _ in repo.list_session_payloads()}
            imported = 0
            for group in groups:
                session_id = group.group_id
                if session_id in existing:
                    continue
                overview = build_group_overview(
                    group.group_id,
                    group.files,
                    start_ts=group.start_ts,
                    end_ts=group.end_ts,
                    grouping_confidence=group.confidence,
                )
                repo.save_session_payload(
                    session_id,
                    {"files": [f.model_dump(mode="json") for f in group.files], "group": overview},
                )
                imported += 1
            repo.commit()

        logger.info("startup_import: done — %d new session(s) imported, %d already existed",
                    imported, len(groups) - imported)
    except Exception:
        logger.exception("startup_import: failed (non-fatal)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.app_env == "local":
        create_all()
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
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


@app.get("/alarm-demo", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
async def alarm_demo():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "alarm_demo.html").read_text(encoding="utf-8")
    return html
