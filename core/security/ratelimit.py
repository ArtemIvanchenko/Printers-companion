import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable

from fastapi import HTTPException, Request
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """Sliding-window limiter, shared across uvicorn workers via Redis.

    With ``--workers N`` an in-process counter is per-process, so the real limit
    is N× the configured value. This uses a Redis sorted-set window keyed per
    client so all workers share one budget. If Redis is unavailable it falls
    back to the in-process counter (fail-open for availability — the limiter is
    a best-effort guard, not a hard security control).
    """

    def __init__(self, max_requests: int = 30, window_sec: int = 60, *, namespace: str = "rl") -> None:
        self.max_requests = max_requests
        self.window_sec = window_sec
        self.namespace = namespace
        self._local: dict[str, list[float]] = defaultdict(list)
        self._redis = None
        self._redis_probed = False

    def _get_redis(self):
        if self._redis_probed:
            return self._redis
        self._redis_probed = True
        try:
            import redis

            from core.config.settings import get_settings

            client = redis.from_url(
                get_settings().redis_url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            client.ping()
            self._redis = client
        except Exception as exc:
            logger.info("Rate limiter using in-process store (Redis unavailable: %s)", exc)
            self._redis = None
        return self._redis

    def check(self, client_key: str) -> None:
        redis_client = self._get_redis()
        if redis_client is not None:
            try:
                self._check_redis(redis_client, client_key)
                return
            except HTTPException:
                raise
            except Exception as exc:
                # Redis hiccup mid-request: don't 500 the caller, fall through to
                # the in-process window so the request is still bounded locally.
                logger.warning("Redis rate-limit check failed (%s); using local fallback", exc)
        self._check_local(client_key)

    def _check_redis(self, redis_client, client_key: str) -> None:
        key = f"{self.namespace}:{client_key}"
        now = time.time()
        window_start = now - self.window_sec
        redis_client.zremrangebyscore(key, 0, window_start)
        count = redis_client.zcard(key)
        if count >= self.max_requests:
            raise HTTPException(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {self.max_requests} requests per {self.window_sec}s",
            )
        pipe = redis_client.pipeline()
        pipe.zadd(key, {f"{now}-{uuid.uuid4().hex}": now})
        pipe.expire(key, self.window_sec)
        pipe.execute()

    def _check_local(self, client_key: str) -> None:
        now = time.monotonic()
        window_start = now - self.window_sec
        timestamps = self._local[client_key]
        timestamps[:] = [t for t in timestamps if t > window_start]
        if len(timestamps) >= self.max_requests:
            raise HTTPException(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {self.max_requests} requests per {self.window_sec}s",
            )
        timestamps.append(now)

    def reset(self, client_key: str) -> None:
        self._local.pop(client_key, None)
        redis_client = self._get_redis()
        if redis_client is not None:
            try:
                redis_client.delete(f"{self.namespace}:{client_key}")
            except Exception as exc:
                logger.warning("Redis rate-limit reset failed: %s", exc)


chat_limiter = SlidingWindowRateLimiter(max_requests=20, window_sec=60, namespace="rl:chat")
# Internal service-to-service: watcher sends 2 req/file × N files on startup
agent_limiter = SlidingWindowRateLimiter(max_requests=200, window_sec=60, namespace="rl:agent")


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
