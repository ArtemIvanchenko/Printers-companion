import time
from collections import defaultdict
from collections.abc import Callable
from functools import wraps

from fastapi import HTTPException, Request
from starlette.status import HTTP_429_TOO_MANY_REQUESTS


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int = 30, window_sec: int = 60):
        self.max_requests = max_requests
        self.window_sec = window_sec
        self._clients: dict[str, list[float]] = defaultdict(list)

    def check(self, client_key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_sec
        timestamps = self._clients[client_key]
        timestamps[:] = [t for t in timestamps if t > window_start]
        if len(timestamps) >= self.max_requests:
            raise HTTPException(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {self.max_requests} requests per {self.window_sec}s",
            )
        timestamps.append(now)

    def reset(self, client_key: str) -> None:
        self._clients.pop(client_key, None)


chat_limiter = SlidingWindowRateLimiter(max_requests=20, window_sec=60)
# Internal service-to-service: watcher sends 2 req/file × N files on startup
agent_limiter = SlidingWindowRateLimiter(max_requests=200, window_sec=60)


def resolve_client_key(request: Request) -> str:
    token = request.headers.get("X-API-Token", "")
    if token:
        return f"token:{token}"
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host}" if request.client else "unknown"


def rate_limit(limiter: SlidingWindowRateLimiter) -> Callable:
    def dependency(request: Request) -> None:
        limiter.check(resolve_client_key(request))
    return dependency
