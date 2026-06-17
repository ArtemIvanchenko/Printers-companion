"""Admin endpoints: version, logs, import status, update history."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

import redis as _redis
from fastapi import APIRouter, Request

from core.config.settings import get_settings
from core.versioning.constants import (
    ANALYSIS_VERSION,
    APP_VERSION,
    RULE_PACK_VERSION,
    SIGNAL_DICTIONARY_VERSION,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_START_TIME = datetime.now(timezone.utc)

_REDIS_KEY = "pla:update:last_event"
_REDIS_TTL = 60 * 60 * 24 * 90


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_client() -> _redis.Redis | None:
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
    return {
        "version": APP_VERSION,
        "git_commit": _git_commit(),
        "build_date": os.environ.get("BUILD_DATE", "unknown"),
        "docker_image": os.environ.get("DOCKER_IMAGE", "dev"),
        "started_at": _START_TIME.isoformat(),
        "components": {
            "analysis": ANALYSIS_VERSION,
            "signal_dictionary": SIGNAL_DICTIONARY_VERSION,
            "rule_pack": RULE_PACK_VERSION,
        },
    }


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(n: int = 200, level: str | None = None) -> list[dict]:
    from core.logging.config import read_recent_logs
    n = min(max(1, n), 1000)
    return await asyncio.get_running_loop().run_in_executor(
        None, read_recent_logs, n, level
    )


# ── Import status ─────────────────────────────────────────────────────────────

@router.get("/import/status")
async def import_status() -> dict:
    from storage.db.session import session_scope
    from storage.repositories.runtime import RuntimeRepository
    try:
        with session_scope() as db:
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


# ── Update history ────────────────────────────────────────────────────────────

@router.get("/update/history")
async def update_history() -> dict:
    """Last recorded update event (written by update.sh after successful git pull)."""
    return await asyncio.get_running_loop().run_in_executor(None, _load_update_event)


@router.post("/update/notify")
async def update_notify(request: Request) -> dict:
    """Called by update.sh after a successful update to record the timestamp."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "commit": body.get("commit", ""),
        "message": body.get("message", ""),
    }
    await asyncio.get_running_loop().run_in_executor(None, _store_update_event, event)
    logger.info("Update notification received: %s", event)
    return {"ok": True}
