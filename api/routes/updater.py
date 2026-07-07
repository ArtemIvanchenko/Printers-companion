"""Version/status endpoints for the dashboard.

Updates happen host-side now (deploy/launch.ps1, triggered from a desktop
shortcut or Windows autostart) — it does `git pull` + `docker compose pull/up`
directly on the operator's PC. This module just reports what's running.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter

from core.versioning.constants import (
    ANALYSIS_VERSION,
    APP_VERSION,
    RULE_PACK_VERSION,
    SIGNAL_DICTIONARY_VERSION,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Time when this API process started (≈ time of last deploy/restart).
_START_TIME = datetime.now(timezone.utc)


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

