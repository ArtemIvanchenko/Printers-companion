from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    if settings.app_env == "local":
        create_all()
    report = run_preflight(settings, component="api")
    for warn in report.warnings:
        import logging
        logging.getLogger("preflight").warning(warn)
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
