"""Admin endpoints: version, logs, import status, update status.

Updates happen ONLY when the operator clicks the desktop icon
(deploy/launch.ps1 -> update.ps1) — there is deliberately no way to trigger
an update from inside the running container. The container has no Docker
access anyway, so it could never actually perform an update; earlier
versions of this file wrote a flag file that a background Task Scheduler
job / launchd agent picked up independently of the icon, which defeated the
"single trigger" design. Don't reintroduce that — see deploy/launch.ps1's
docstring for the intended flow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

import httpx
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
_REDIS_KEY   = "pla:update:last_event"
_REDIS_TTL   = 60 * 60 * 24 * 90

_GITHUB_REPO   = "ArtemIvanchenko/Printers-companion"
_GITHUB_BRANCH = "main"


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _redis_client() -> _redis.Redis | None:
    try:
        s = get_settings()
        return _redis.from_url(s.redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
    except Exception:
        logger.debug("Redis client unavailable", exc_info=True)
        return None


def _store_update_event(event: dict) -> None:
    try:
        r = _redis_client()
        if r:
            r.set(_REDIS_KEY, json.dumps(event), ex=_REDIS_TTL)
    except Exception as exc:
        logger.warning("Could not store update event: %s", exc)


def _load_update_event() -> dict:
    try:
        r = _redis_client()
        if r:
            raw = r.get(_REDIS_KEY)
            if raw:
                return json.loads(raw)
    except Exception as exc:
        logger.warning("Could not load update event: %s", exc)
    return {}


# ── Version ───────────────────────────────────────────────────────────────────

def _git_commit() -> str:
    if c := os.environ.get("GIT_COMMIT", "").strip():
        return c[:8]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True
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
    return await asyncio.get_running_loop().run_in_executor(None, read_recent_logs, n, level)


# ── Import status ─────────────────────────────────────────────────────────────

@router.get("/import/status")
def import_status() -> dict:  # sync: queries the DB, must not run on the loop
    from storage.db.session import session_scope
    from storage.repositories.runtime import RuntimeRepository
    try:
        with session_scope() as db:
            repo = RuntimeRepository(db)
            # Only the count is displayed — don't deserialise every payload for it.
            session_count = len(repo.list_session_ids())
            node_id = get_settings().compute_node_id
            import_job_count = repo.count_import_jobs(owner_node_id=node_id)
            last_job = repo.latest_import_job(owner_node_id=node_id)
        return {
            "session_count": session_count,
            "import_job_count": import_job_count,
            "last_import_at": last_job.updated_at.isoformat() if last_job else None,
            "last_import_status": last_job.status.value if last_job else None,
            "last_import_name": last_job.source_name if last_job else None,
        }
    except Exception as exc:
        logger.warning("import_status failed: %s", exc)
        return {"session_count": 0, "import_job_count": 0}


# ── Update: check ─────────────────────────────────────────────────────────────

@router.get("/update/check")
async def check_for_update() -> dict:
    """Compare running GIT_COMMIT with the latest commit on GitHub main."""
    current = _git_commit()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/commits/{_GITHUB_BRANCH}",
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            r.raise_for_status()
            data = r.json()
        latest_sha   = data["sha"]
        latest_short = latest_sha[:8]
        version_unknown = current in ("unknown", "")
        update_available = not version_unknown and current != latest_short
        return {
            "update_available": update_available,
            "version_unknown": version_unknown,
            "current_commit": current,
            "latest_commit": latest_short,
            "latest_date": data["commit"]["committer"]["date"],
            "latest_message": data["commit"]["message"].split("\n")[0][:80],
        }
    except Exception as exc:
        logger.warning("GitHub update check failed: %s", exc)
        return {"update_available": False, "error": str(exc), "current_commit": current}


# ── Update: history ───────────────────────────────────────────────────────────

@router.get("/update/history")
async def update_history() -> dict:
    """Last recorded update event."""
    return await asyncio.get_running_loop().run_in_executor(None, _load_update_event)


@router.post("/update/notify")
async def update_notify(request: Request) -> dict:
    """Called by deploy/update.ps1 (via the desktop icon) after a successful
    local update, to record the timestamp for the dashboard's version card."""
    try:
        body = await request.json()
    except Exception:
        logger.debug("update_notify received an invalid JSON body; treating as empty", exc_info=True)
        body = {}
    event = {
        "at":      datetime.now(timezone.utc).isoformat(),
        "commit":  body.get("commit", ""),
        "message": body.get("message", ""),
        "source":  "script",
    }
    await asyncio.get_running_loop().run_in_executor(None, _store_update_event, event)
    return {"ok": True}
