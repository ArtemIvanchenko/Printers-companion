import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.routes import (
    agent,
    analysis,
    anomalies,
    updater,
    background,
    chat,
    dashboard,
    exports,
    imports,
    insights,
    knowledge,
    llm,
    machine_settings,
    maintenance,
    operator_events,
    operator_journal,
    powder,
    prints,
    profiles,
    quality,
    realtime,
    sessions,
    uploads,
    web,
)
from core.config.settings import get_settings
from core.logging.config import RequestIDMiddleware, configure_logging
from core.preflight import run_preflight, exit_on_failure
from core.versioning.version import APP_VERSION
from storage.db.migrate import upgrade_to_head

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
        from storage.db.session import session_scope
        from storage.repositories.runtime import RuntimeRepository

        registry = build_registry()
        profile = get_profile()
        result = IngestionService(registry, profile).parse(path)
        groups = group_files_into_sessions(result.files)

        if not groups:
            logger.info("startup_import: no log groups found in %s", path)
            return

        logger.info("startup_import: found %d session group(s)", len(groups))

        with session_scope() as db:
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
                    # Strip parse_result (events): keeps the payload tiny; events
                    # are re-read from disk on demand. Avoids ~96 MB/session.
                    {"files": [f.model_dump(mode="json", exclude={"parse_result"}) for f in group.files], "group": overview},
                )
                imported += 1
            # save_session_payload already commits each row; no extra commit needed

            from domain.services.print_linking import auto_link_print_records

            links = auto_link_print_records(db)  # session_scope commits at the boundary

        logger.info("startup_import: done — %d new session(s) imported, %d already existed, %d linked to print records",
                    imported, len(groups) - imported, len(links))
    except Exception:
        logger.exception("startup_import: failed (non-fatal)")


async def _startup_llm_discovery() -> None:
    """Auto-connect to a local LM Studio server without blocking startup.

    Replaces the old blocking probe that ran at Settings construction (and held
    up every process import for up to ~8s). Runs once, in the background.
    """
    if settings.llm_provider in ("null", "none", ""):
        return
    try:
        from reporting.llm.discovery import discover_lmstudio

        result = await discover_lmstudio(preferred_model=settings.llm_model)
        if result.available and result.base_url:
            settings.llm_base_url = result.base_url
            if result.selected_model:
                settings.llm_model = result.selected_model
            logger.info("startup: LM Studio discovered at %s (model=%s)", settings.llm_base_url, settings.llm_model)
        else:
            logger.info("startup: no LM Studio auto-discovered (%s); using configured %s",
                        result.error, settings.llm_base_url)
    except Exception:
        logger.exception("startup: LM Studio discovery failed (non-fatal)")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Local dev (single process) auto-migrates here; prod runs `alembic upgrade
    # head` once in the container entrypoint before workers spawn (race-free).
    if settings.app_env == "local":
        upgrade_to_head()
    report = run_preflight(settings, component="api")
    for warn in report.warnings:
        logging.getLogger("preflight").warning(warn)
    # Refuse to start in production with default credentials / failed checks.
    exit_on_failure(report)

    # Best-effort: create all MinIO buckets so file uploads work immediately.
    try:
        from storage.object_store.minio_client import ObjectStore

        store = ObjectStore()
        if store.is_available():
            store.ensure_all_buckets()
        else:
            logger.warning("startup: MinIO unavailable — buckets not ensured")
    except Exception:
        logger.exception("startup: ensure_all_buckets failed (non-fatal)")

    # Kick off background import of existing log files.
    task = asyncio.create_task(_startup_import(settings.raw_logs_container_path))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)

    # Non-blocking LM Studio auto-discovery (was a blocking probe at import time).
    llm_task = asyncio.create_task(_startup_llm_discovery())
    _BG_TASKS.add(llm_task)
    llm_task.add_done_callback(_BG_TASKS.discard)

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
if origins:
    cors_kwargs = {"allow_origins": origins, "allow_credentials": True}
else:
    # Never combine a wildcard origin with credentials (browsers reject it, and
    # it would be a security hole if they didn't). No configured origins → block
    # cross-origin requests rather than silently opening to "*".
    logger.warning("CORS_ORIGINS is empty — cross-origin browser requests will be blocked")
    cors_kwargs = {"allow_origins": [], "allow_credentials": False}
app.add_middleware(
    CORSMiddleware,
    allow_methods=["*"],
    allow_headers=["*"],
    **cors_kwargs,
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: "Request", exc: Exception) -> "JSONResponse":
    """Catch-all so unexpected errors return a clean JSON 500 (with the request id
    for log correlation) instead of leaking a stack trace."""
    request_id = getattr(request.state, "request_id", None)
    logger.exception("Unhandled error on %s %s (request_id=%s)", request.method, request.url.path, request_id)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up. Cheap, never touches dependencies."""
    return {"status": "ok", "version": APP_VERSION, "llm_provider": settings.llm_provider}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    """Readiness: verify the backing services (PostgreSQL, Redis, MinIO) respond.

    Returns 503 if any dependency is unreachable so orchestrators don't route
    traffic to a pod that can't actually serve requests."""
    checks: dict[str, bool] = {}

    try:
        from sqlalchemy import text
        from storage.db.session import session_scope

        with session_scope() as db:
            db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        logger.exception("readiness: database check failed")
        checks["database"] = False

    try:
        import redis as _redis

        client = _redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        checks["redis"] = bool(client.ping())
    except Exception:
        logger.exception("readiness: redis check failed")
        checks["redis"] = False

    try:
        from storage.object_store.minio_client import ObjectStore

        checks["minio"] = ObjectStore().is_available()
    except Exception:
        logger.exception("readiness: minio check failed")
        checks["minio"] = False

    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks, "version": APP_VERSION},
    )


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
app.include_router(prints.router)
app.include_router(exports.router)
app.include_router(machine_settings.router)


@app.get("/alarm-demo", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
async def alarm_demo():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "alarm_demo.html").read_text(encoding="utf-8")
    return html
