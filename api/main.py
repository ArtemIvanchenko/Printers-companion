import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    test_metrics,
    uploads,
    web,
)
from core.config.settings import get_settings
from core.compute_identity import register_compute_node
from core.logging.config import RequestIDMiddleware, configure_logging
from core.preflight import run_preflight, exit_on_failure
from core.versioning.version import APP_VERSION
from storage.db.migrate import assert_schema_at_head, upgrade_to_head

logger = logging.getLogger(__name__)

# Strong references so background tasks aren't GC-collected mid-run.
_BG_TASKS: set[asyncio.Task] = set()


def _startup_import(raw_logs_path: str) -> None:
    """On startup, durably enqueue raw-log candidates through the normal path."""
    path = Path(raw_logs_path)
    if not path.exists() or not path.is_dir():
        logger.warning("startup_import: raw-logs path not found: %s", path)
        return

    logger.info("startup_import: scanning %s for import candidates …", path)
    try:
        from api.routes.uploads import _trigger_rescan

        # Existing flat log folders must be one import batch. Enumerating every
        # child here produced hundreds of confirmations and prevented files
        # from the same dated print from reaching the grouping algorithm
        # together.
        jobs = _trigger_rescan(raw_logs_path, candidates=[path])
        waiting = sum(job["status"] == "awaiting_operator_confirmation" for job in jobs)
        logger.info(
            "startup_import: %d durable job(s), %d awaiting confirmation",
            len(jobs),
            waiting,
        )
    except Exception:
        logger.exception("startup_import: failed (non-fatal)")


async def _startup_import_once(raw_logs_path: str) -> None:
    """Run ``_startup_import`` once per container, off the event loop.

    Waits 15 s so the database finishes initialising, then takes a cross-worker
    claim: uvicorn runs several workers and each executes the lifespan, so
    without it every worker would parse the whole folder at once and race to
    insert the same sessions.
    """
    from core.locks import once_across_workers

    await asyncio.sleep(15)

    def _guarded() -> None:
        with once_across_workers("startup_import", ttl_sec=3600) as mine:
            if mine:
                _startup_import(raw_logs_path)

    await asyncio.to_thread(_guarded)


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
    # Local dev (single process) auto-migrates here. NAS/operator production
    # uses the explicit one-shot migrator; API performs a read-only head check.
    if settings.app_env == "local":
        upgrade_to_head()
    report = run_preflight(settings, component="api")
    for warn in report.warnings:
        logging.getLogger("preflight").warning(warn)
    # Refuse to start in production with default credentials / failed checks.
    exit_on_failure(report)
    if settings.app_env not in ("local", "test"):
        assert_schema_at_head()
        instance_id = register_compute_node(settings)
        logger.info(
            "compute node %s registered to workstation %s",
            settings.compute_node_id,
            instance_id[:12],
        )

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

    # Optional legacy-root scan. NAS/operator deployments disable it: each
    # upload batch is already enqueued explicitly, and rescanning the whole
    # historical mount after every restart would overlap those jobs.
    if settings.startup_import_enabled:
        task = asyncio.create_task(_startup_import_once(settings.raw_logs_container_path))
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
app.include_router(test_metrics.router)


@app.get("/alarm-demo", response_class=__import__("fastapi.responses", fromlist=["HTMLResponse"]).HTMLResponse)
async def alarm_demo():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "web_templates" / "alarm_demo.html").read_text(encoding="utf-8")
    return html
