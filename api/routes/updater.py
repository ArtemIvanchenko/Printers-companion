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

import httpx
from fastapi import APIRouter, HTTPException

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
