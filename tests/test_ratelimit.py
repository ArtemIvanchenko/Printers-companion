"""Rate limiter behaviour in the in-process fallback path (no Redis in tests).

The limiter shares a window across uvicorn workers via Redis when available and
falls back to a per-process window otherwise. These tests pin the fallback
semantics so the security guard can't silently regress.
"""
import pytest
from fastapi import HTTPException

from core.security.ratelimit import SlidingWindowRateLimiter


def _limiter() -> SlidingWindowRateLimiter:
    lim = SlidingWindowRateLimiter(max_requests=3, window_sec=60, namespace="rl:test")
    # Force the in-process path regardless of any ambient Redis.
    lim._redis = None
    lim._redis_probed = True
    return lim


def test_allows_up_to_limit_then_blocks() -> None:
    lim = _limiter()
    for _ in range(3):
        lim.check("client-a")
    with pytest.raises(HTTPException) as exc:
        lim.check("client-a")
    assert exc.value.status_code == 429


def test_clients_are_isolated() -> None:
    lim = _limiter()
    for _ in range(3):
        lim.check("client-a")
    # A different client has its own budget.
    lim.check("client-b")


def test_reset_clears_the_window() -> None:
    lim = _limiter()
    for _ in range(3):
        lim.check("client-a")
    lim.reset("client-a")
    lim.check("client-a")  # must not raise after reset
