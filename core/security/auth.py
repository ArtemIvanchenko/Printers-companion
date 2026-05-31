import hmac
from enum import StrEnum
from typing import Annotated

from fastapi import Header, HTTPException, status

from core.config.settings import get_settings


class Role(StrEnum):
    operator = "operator"
    engineer = "engineer"
    admin = "admin"
    service = "service"
    viewer = "viewer"
    agent_service = "agent_service"


def _constant_time_compare(a: str, b: str) -> bool:
    """
    Compare two strings in constant time to prevent timing attacks.
    Uses HMAC for constant-time comparison.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a, b)


def require_service_token(
    x_api_token: Annotated[str | None, Header(alias="X-API-Token")] = None,
) -> Role:
    settings = get_settings()
    
    # Use constant-time comparison to prevent timing attacks
    is_service_token = _constant_time_compare(
        x_api_token or "",
        settings.api_service_token
    )
    is_agent_token = _constant_time_compare(
        x_api_token or "",
        settings.agent_api_token
    )
    
    if is_service_token:
        return Role.service
    elif is_agent_token:
        return Role.agent_service
    
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")

