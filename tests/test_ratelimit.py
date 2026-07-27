"""Rate limiter: window behaviour, key resolution and bounded memory."""
import pytest
from fastapi import HTTPException

from core.security.ratelimit import SlidingWindowRateLimiter, resolve_client_key


class _Client:
    def __init__(self, host: str) -> None:
        self.host = host


class _Request:
    def __init__(self, headers: dict[str, str], host: str | None = "10.0.0.1") -> None:
        self.headers = headers
        self.client = _Client(host) if host else None


def test_allows_up_to_the_limit_then_rejects():
    limiter = SlidingWindowRateLimiter(max_requests=3, window_sec=60)
    for _ in range(3):
        limiter.check("ip:1.2.3.4")
    with pytest.raises(HTTPException) as exc:
        limiter.check("ip:1.2.3.4")
    assert exc.value.status_code == 429


def test_clients_are_counted_independently():
    limiter = SlidingWindowRateLimiter(max_requests=1, window_sec=60)
    limiter.check("ip:1.1.1.1")
    limiter.check("ip:2.2.2.2")  # must not raise


def test_forwarded_for_header_cannot_mint_fresh_buckets():
    """Regression: X-Forwarded-For was trusted with no proxy in front, so a
    client could rotate it to get a new bucket per request — bypassing the limit
    and growing the key map without bound."""
    keys = {
        resolve_client_key(_Request({"X-Forwarded-For": f"9.9.9.{i}"}, host="10.0.0.1"))
        for i in range(50)
    }
    assert keys == {"ip:10.0.0.1"}


def test_api_token_still_identifies_the_caller():
    assert resolve_client_key(_Request({"X-API-Token": "abc"})) == "token:abc"


def test_idle_clients_are_evicted():
    """Regression: the key map only ever grew, one entry per distinct client
    key the process had ever seen."""
    limiter = SlidingWindowRateLimiter(max_requests=5, window_sec=1)
    for i in range(100):
        limiter.check(f"ip:10.0.0.{i}")
    assert limiter.tracked_clients == 100

    # Age every recorded hit past the eviction horizon, then make one request.
    for hits in limiter._clients.values():
        hits[:] = [t - 10_000 for t in hits]
    limiter._last_sweep = float("-inf")
    limiter.check("ip:fresh")

    assert limiter.tracked_clients == 1


def test_active_clients_are_not_evicted():
    limiter = SlidingWindowRateLimiter(max_requests=5, window_sec=60)
    limiter.check("ip:active")
    limiter._last_sweep = float("-inf")
    limiter.check("ip:other")
    assert limiter.tracked_clients == 2
