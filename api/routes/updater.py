"""On-demand update endpoint — proxies to Watchtower's HTTP API.

Watchtower pulls new Docker images from Docker Hub and restarts containers.
The web dashboard calls POST /admin/update; we fire the Watchtower request
in the background (fire-and-forget) because it holds the connection open for
the full duration of the pull (potentially 5-10 minutes).

Auto-update: Watchtower also polls Docker Hub every hour automatically
(WATCHTOWER_POLL_INTERVAL=3600 + WATCHTOWER_HTTP_API_PERIODIC_POLLS=true).
When it applies an update it calls POST /admin/update/notify (shoutrrr webhook)
so we can record the timestamp and show it in the dashboard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
from datetime import datetime, timezone

import httpx
import redis as _redis
from fastapi import APIRouter, HTTPException, Request

from core.config.settings import get_settings
from core.versioning.constants import (
    ANALYSIS_VERSION,
    APP_VERSION,
    RULE_PACK_VERSION,
    SIGNAL_DICTIONARY_VERSION,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_WATCHTOWER_HOST = "watchtower"
_WATCHTOWER_PORT = 8080
_WATCHTOWER_URL  = f"http://{_WATCHTOWER_HOST}:{_WATCHTOWER_PORT}"
_WATCHTOWER_TOKEN = os.environ.get("WATCHTOWER_TOKEN", "pla-watchtower-token")

# Time when this API process started (≈ time of last deploy/restart).
_START_TIME = datetime.now(timezone.utc)

# Redis key for last update event; TTL = 90 days.
_REDIS_KEY = "pla:update:last_event"
_REDIS_TTL = 60 * 60 * 24 * 90

# Strong references keep fire-and-forget tasks alive until done.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# ── Redis helpers (sync, best-effort) ────────────────────────────────────────

def _redis_client() -> _redis.Redis | None:
    """Return a short-timeout Redis client, or None if unreachable."""
    try:
        settings = get_settings()
        return _redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
    except Exception:
        return None


def _store_update_event(event: dict) -> None:
    try:
        r = _redis_client()
        if r:
            r.set(_REDIS_KEY, json.dumps(event), ex=_REDIS_TTL)
    except Exception as exc:
        logger.warning("Could not store update event in Redis: %s", exc)


def _load_update_event() -> dict:
    try:
        r = _redis_client()
        if r:
            raw = r.get(_REDIS_KEY)
            if raw:
                return json.loads(raw)
    except Exception as exc:
        logger.warning("Could not load update event from Redis: %s", exc)
    return {}


# ── Version ───────────────────────────────────────────────────────────────────

def _git_commit() -> str:
    """Short git commit hash (injected at Docker build, or read from git)."""
    if c := os.environ.get("GIT_COMMIT", "").strip():
        return c[:8]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


@router.get("/version")
async def get_version() -> dict:
    """Running application version + component manifests + uptime info."""
    return {
        "version": APP_VERSION,
        "git_commit": _git_commit(),
        "build_date": os.environ.get("BUILD_DATE", "unknown"),
        "docker_image": os.environ.get("DOCKER_IMAGE", "dev"),
        "started_at": _START_TIME.isoformat(),
        "auto_update_interval_sec": 3600,
        "components": {
            "analysis": ANALYSIS_VERSION,
            "signal_dictionary": SIGNAL_DICTIONARY_VERSION,
            "rule_pack": RULE_PACK_VERSION,
        },
    }


# ── Update history ────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(n: int = 200, level: str | None = None) -> list[dict]:
    """Return the last *n* structured log lines from the rotating log file.

    Query params:
      n     – number of entries (max 1000, default 200)
      level – filter by level: DEBUG / INFO / WARNING / ERROR
    """
    from core.logging.config import read_recent_logs
    n = min(max(1, n), 1000)
    return await asyncio.get_running_loop().run_in_executor(
        None, read_recent_logs, n, level
    )


@router.get("/import/status")
async def import_status() -> dict:
    """Quick summary for the dashboard status card: session count + last import."""
    from storage.db.session import SessionLocal
    from storage.repositories.runtime import RuntimeRepository
    try:
        with SessionLocal() as db:
            repo = RuntimeRepository(db)
            sessions = repo.list_session_payloads()
            jobs = repo.list_import_jobs()
        last_job = max(jobs, key=lambda j: j.updated_at, default=None)
        return {
            "session_count": len(sessions),
            "import_job_count": len(jobs),
            "last_import_at": last_job.updated_at.isoformat() if last_job else None,
            "last_import_status": last_job.status.value if last_job else None,
            "last_import_name": last_job.source_name if last_job else None,
        }
    except Exception as exc:
        logger.warning("import_status failed: %s", exc)
        return {"session_count": 0, "import_job_count": 0}


@router.get("/update/history")
async def update_history() -> dict:
    """Last recorded update event (auto via Watchtower webhook or manual trigger)."""
    return await asyncio.get_running_loop().run_in_executor(None, _load_update_event)


@router.post("/update/notify")
async def watchtower_notify(request: Request) -> dict:
    """Watchtower shoutrrr generic webhook — called after each auto-update.

    Watchtower config (docker-compose.yml):
      WATCHTOWER_NOTIFICATION_URL: "generic+http://api:8000/admin/update/notify"
      WATCHTOWER_NOTIFICATIONS_LEVEL: "info"
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "source": "auto",
        "message": body.get("message", ""),
    }
    await asyncio.get_running_loop().run_in_executor(None, _store_update_event, event)
    logger.info("Auto-update notification received from Watchtower")
    return {"ok": True}


# ── Watchtower TCP check ───────────────────────────────────────────────────────

def _watchtower_reachable() -> bool:
    """TCP-level check — avoids HTTP timeout from a blocking update call."""
    try:
        with socket.create_connection((_WATCHTOWER_HOST, _WATCHTOWER_PORT), timeout=3):
            return True
    except OSError:
        return False


@router.get("/update/status")
async def update_status() -> dict:
    """Check whether Watchtower is reachable (TCP ping — non-blocking)."""
    online = await asyncio.get_running_loop().run_in_executor(None, _watchtower_reachable)
    return {"watchtower": "online" if online else "offline"}


# ── Manual trigger ─────────────────────────────────────────────────────────────

async def _fire_watchtower() -> None:
    """Send update request to Watchtower in the background (fire-and-forget)."""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            await client.post(
                f"{_WATCHTOWER_URL}/v1/update",
                headers={"Authorization": f"Bearer {_WATCHTOWER_TOKEN}"},
            )
    except Exception as exc:
        logger.info("Watchtower update request ended: %s", exc)


@router.post("/update")
async def trigger_update() -> dict:
    """Kick off a Docker Hub pull + container restart via Watchtower.

    Returns immediately — the actual update runs in the background.
    The dashboard polls /health to detect when the API is back online.
    """
    if not await asyncio.get_running_loop().run_in_executor(None, _watchtower_reachable):
        raise HTTPException(
            status_code=503,
            detail="Watchtower недоступен. Убедитесь что контейнер watchtower запущен.",
        )

    # Record manual trigger immediately (before the actual pull starts).
    event = {"at": datetime.now(timezone.utc).isoformat(), "source": "manual"}
    await asyncio.get_running_loop().run_in_executor(None, _store_update_event, event)

    task = asyncio.create_task(_fire_watchtower())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    logger.info("Watchtower update triggered (background)")

    return {
        "ok": True,
        "message": (
            "Обновление запущено. Образы загружаются с Docker Hub. "
            "Сервисы перезапустятся автоматически через 1–5 минут."
        ),
    }
