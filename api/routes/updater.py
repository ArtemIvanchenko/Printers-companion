"""On-demand update endpoint — proxies to Watchtower's HTTP API.

Watchtower pulls new Docker images from Docker Hub and restarts containers.
The web dashboard calls POST /admin/update; we fire the Watchtower request
in the background (fire-and-forget) because it holds the connection open for
the full duration of the pull (potentially 5-10 minutes).

The dashboard JS then polls /health until the API comes back after restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess

import httpx
from fastapi import APIRouter, HTTPException

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

# The event loop keeps only weak references to bare tasks, so a fire-and-forget
# task can be garbage-collected (and cancelled) mid-pull.  Hold a strong ref
# until each task finishes.  See https://docs.python.org/3/library/asyncio-task.html
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _git_commit() -> str:
    """Short git commit hash baked into the running process (best-effort)."""
    # In Docker, injected as GIT_COMMIT build-arg → env var.
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
    """Return the running application version and component manifests."""
    return {
        "version": APP_VERSION,
        "git_commit": _git_commit(),
        "build_date": os.environ.get("BUILD_DATE", "unknown"),
        "docker_image": os.environ.get("DOCKER_IMAGE", "dev"),
        "components": {
            "analysis": ANALYSIS_VERSION,
            "signal_dictionary": SIGNAL_DICTIONARY_VERSION,
            "rule_pack": RULE_PACK_VERSION,
        },
    }


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


async def _fire_watchtower() -> None:
    """Send the update request to Watchtower in the background.

    Watchtower holds the connection open for the full pull duration — we set
    a generous timeout and ignore the result; the containers will restart when
    new images are ready regardless of whether we read the response.
    """
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

    # Fire and forget — don't await, return immediately to the browser.
    # Keep a strong reference so the GC can't cancel the task mid-pull.
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
