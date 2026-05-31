from typing import Any

import httpx

from core.config.settings import get_settings


def _headers() -> dict[str, str]:
    return {"X-API-Token": get_settings().agent_api_token}


def _api_url(path: str) -> str:
    return f"{get_settings().internal_api_url.rstrip('/')}{path}"


async def api_post(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(_api_url(path), json=payload or {}, headers=_headers())
        response.raise_for_status()
        return response.json()


async def api_patch(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.patch(_api_url(path), json=payload or {}, headers=_headers())
        response.raise_for_status()
        return response.json()


async def api_get(path: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(_api_url(path), headers=_headers())
        response.raise_for_status()
        return response.json()
