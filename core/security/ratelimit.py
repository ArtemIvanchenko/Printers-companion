"""In-process sliding-window rate limiting.

Scope note: state lives in the worker process, so with N uvicorn workers the
effective allowance is up to N× the configured one. That is accepted here — the
limiter exists to stop a runaway client or a looping script from hammering the
LLM endpoint, not as a security control, and this deployment runs a couple of
workers for a handful of operators. A shared limit would need Redis.
"""
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

# Stop tracking a client this long after its last request. Without eviction the
# key map grows for the process's whole lifetime — one entry per distinct client
# key ever seen, which an attacker can mint at will.
_IDLE_EVICTION_SEC = 300
# Hard ceiling on tracked clients, in case eviction cannot keep up.
_MAX_TRACKED_CLIENTS = 10_000


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int = 30, window_sec: int = 60):
        self.max_requests = max_requests
        self.window_sec = window_sec
        self._clients: dict[str, list[float]] = {}
        # Sync handlers run in a threadpool, so several requests can hit the
        # same key concurrently; the read-modify-write below must be atomic.
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def _sweep(self, now: float) -> None:
        """Drop clients with no request inside the eviction horizon."""
        cutoff = now - max(self.window_sec, _IDLE_EVICTION_SEC)
        stale = [key for key, hits in self._clients.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._clients[key]
        self._last_sweep = now

    def check(self, client_key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_sec
        with self._lock:
            if now - self._last_sweep > _IDLE_EVICTION_SEC or len(self._clients) > _MAX_TRACKED_CLIENTS:
                self._sweep(now)

            timestamps = self._clients.setdefault(client_key, [])
            timestamps[:] = [t for t in timestamps if t > window_start]
            if len(timestamps) >= self.max_requests:
                raise HTTPException(
                    status_code=HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded: {self.max_requests} requests per {self.window_sec}s",
                )
            timestamps.append(now)

    def reset(self, client_key: str) -> None:
        with self._lock:
            self._clients.pop(client_key, None)

    @property
    def tracked_clients(self) -> int:
        return len(self._clients)


chat_limiter = SlidingWindowRateLimiter(max_requests=20, window_sec=60)
# Internal service-to-service: watcher sends 2 req/file × N files on startup
agent_limiter = SlidingWindowRateLimiter(max_requests=200, window_sec=60)


def resolve_client_key(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.

    X-Forwarded-For is deliberately NOT trusted: nothing in this stack terminates
    in front of the API, so the header is attacker-controlled — rotating it gave
    a fresh bucket per request, which both bypassed the limit entirely and grew
    the key map without bound. Add a trusted-proxy allowlist here if a reverse
    proxy is ever put in front.
    """
    token = request.headers.get("X-API-Token", "")
    if token:
        return f"token:{token}"
    return f"ip:{request.client.host}" if request.client else "unknown"


def rate_limit(limiter: SlidingWindowRateLimiter) -> Callable:
    def dependency(request: Request) -> None:
        limiter.check(resolve_client_key(request))
    return dependency
