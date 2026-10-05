"""Local admission barrier during a journalled host update (not a NAS lock)."""
from __future__ import annotations

import os
from pathlib import Path
from contextlib import contextmanager
import time

UPDATE_PROTOCOL = 1
WRITE_DRAIN_SUPPORTED = os.name == 'posix'


def maintenance_file() -> Path:
    return Path(os.environ.get("OPERATOR_INSTANCE_FILE", "/var/lib/printer-companion/instance-id")).parent / "operator-update.json"


def maintenance_active() -> bool:
    try:
        return maintenance_file().exists()
    except OSError:
        return True  # Fail closed; an unreadable local barrier is not permission.


def _admission_stream():
    path = maintenance_file().with_name('operator-write.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    return os.fdopen(descriptor, 'a+b')


@contextmanager
def write_admission():
    """Keep a shared OS lock through the entire write response/background work.

    All API processes in Docker share this file. Checking the marker *after*
    taking the lock closes the admission race; the host then waits for an
    exclusive lock before switching containers. Native Windows API development
    is not a supported host-update target (operator API always runs in Docker).
    """
    if not WRITE_DRAIN_SUPPORTED:
        yield not maintenance_active()
        return
    import fcntl
    try:
        stream = _admission_stream()
    except OSError:
        yield False
        return
    with stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield not maintenance_active()
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def drain_writes(timeout: float = 60) -> None:
    """Called in a short-lived Linux container exec, never on the event loop."""
    if not WRITE_DRAIN_SUPPORTED or not maintenance_active():
        raise RuntimeError('Write drain requires the local maintenance barrier.')
    import fcntl
    with _admission_stream() as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Active write requests have not finished.') from None
                time.sleep(0.05)
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class UpdateAdmissionMiddleware:
    """ASGI, not BaseHTTPMiddleware: streaming uploads/background writes drain."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            return await self.app(scope, receive, send)
        with write_admission() as admitted:
            if admitted:
                return await self.app(scope, receive, send)
            from starlette.responses import JSONResponse
            response = JSONResponse(status_code=503, headers={'Retry-After': '10'}, content={
                'detail': 'Этот ПК обновляется. Дождитесь завершения; загрузки и расчёты временно не принимаются.',
                'code': 'operator_update_in_progress',
            })
            await response(scope, receive, send)
