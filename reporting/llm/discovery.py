"""Live LM Studio discovery.

Detects a running LM Studio (OpenAI-compatible) server on the local host and
reports which models are loaded, so the system can auto-connect on demand —
complementing the one-shot discovery done at settings-construction time.
"""
import logging
from dataclasses import dataclass, field

import httpx

from core.config.settings import LMSTUDIO_CANDIDATE_URLS

logger = logging.getLogger(__name__)


@dataclass
class LMStudioDiscovery:
    available: bool = False
    base_url: str | None = None
    models: list[str] = field(default_factory=list)
    selected_model: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "base_url": self.base_url,
            "models": self.models,
            "selected_model": self.selected_model,
            "error": self.error,
        }


def select_model(models: list[str], preferred: str) -> str | None:
    """Pick the configured model if it is loaded, otherwise the first loaded one."""
    if not models:
        return None
    return preferred if preferred in models else models[0]


async def probe_lmstudio(base_url: str, timeout: float = 2.0) -> list[str] | None:
    """Probe one server's ``/models`` endpoint.

    Returns the list of loaded model ids (possibly empty if the server is up but
    has no model loaded), or ``None`` if the server is unreachable.
    """
    url = f"{base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"User-Agent": "printer-log-analytics/1.0"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.debug("LM Studio probe failed for %s: %s", base_url, exc)
        return None
    return [m.get("id") for m in data.get("data", []) if m.get("id")]


async def discover_lmstudio(
    candidates: list[str] | None = None,
    preferred_model: str = "",
    timeout: float = 2.0,
) -> LMStudioDiscovery:
    """Probe candidate URLs and return the first server with a usable model."""
    urls = candidates if candidates is not None else list(LMSTUDIO_CANDIDATE_URLS)
    empty_server: str | None = None
    for base_url in urls:
        models = await probe_lmstudio(base_url, timeout=timeout)
        if models is None:
            continue  # unreachable
        if not models:
            empty_server = empty_server or base_url
            continue
        return LMStudioDiscovery(
            available=True,
            base_url=base_url,
            models=models,
            selected_model=select_model(models, preferred_model),
        )
    if empty_server:
        return LMStudioDiscovery(
            available=False,
            base_url=empty_server,
            error="server reachable but no model loaded",
        )
    return LMStudioDiscovery(available=False, error="no LM Studio server reachable")
