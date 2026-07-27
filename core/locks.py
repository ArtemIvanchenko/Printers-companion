"""Cross-process advisory locks.

The API runs several uvicorn workers, each of which executes the FastAPI
lifespan. Anything that must happen once per *container* — not once per worker —
has to coordinate outside the process. Redis is already a hard dependency of the
stack, so it is the coordination point.

Fails open: if Redis is unreachable the lock is granted. The callers here guard
idempotent work, so a duplicated run is wasteful but not incorrect, whereas
skipping it entirely would silently drop the startup import.
"""
from __future__ import annotations

import logging
import os
import socket
from contextlib import contextmanager
from collections.abc import Generator

logger = logging.getLogger(__name__)

_OWNER = f"{socket.gethostname()}:{os.getpid()}"


@contextmanager
def once_across_workers(name: str, ttl_sec: int = 900) -> Generator[bool, None, None]:
    """Yield True in exactly one worker, False in the others.

    ``ttl_sec`` bounds how long the claim survives — a worker killed mid-task
    must not block the next container start forever, so pick a value comfortably
    longer than the guarded work.
    """
    from core.config.settings import get_settings

    key = f"pla:once:{name}"
    client = None
    acquired = True
    try:
        import redis as _redis

        client = _redis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
        acquired = bool(client.set(key, _OWNER, nx=True, ex=ttl_sec))
    except Exception:
        logger.warning("once_across_workers(%s): Redis unavailable, running unguarded", name,
                       exc_info=True)
        client = None
        acquired = True

    if not acquired:
        logger.info("once_across_workers(%s): another worker holds the claim — skipping", name)

    try:
        yield acquired
    finally:
        # Release only our own claim, so a slow run that outlived the TTL cannot
        # delete a claim a later worker has since taken.
        if client is not None and acquired:
            try:
                if client.get(key) == _OWNER:
                    client.delete(key)
            except Exception:
                logger.debug("once_across_workers(%s): release failed", name, exc_info=True)
