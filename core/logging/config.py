import json
import logging
import logging.handlers
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from core.logging.context import RequestIDFilter, request_id_var

# Path inside the container — mounted as a host volume in docker-compose.
LOG_DIR = Path(os.environ.get("LOG_DIR", "/app/logs"))
LOG_FILE = LOG_DIR / "app.log"
# Max 10 MB per file, keep 5 rotated files → ≤ 50 MB total.
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "request_id") and record.request_id:
            entry["request_id"] = record.request_id
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        rid = request.headers.get("X-Request-ID", "")
        if not rid:
            import uuid
            rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)


def configure_logging(level: str = "INFO") -> None:
    fmt = StructuredFormatter()
    flt = RequestIDFilter()

    # Always log to stdout (Docker logs / docker compose logs).
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    stdout_handler.addFilter(flt)
    handlers: list[logging.Handler] = [stdout_handler]

    # Also write to a rotating file so the dashboard can show recent logs
    # and operators can grep without attaching to the container.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(flt)
        handlers.append(file_handler)
    except OSError:
        # /app/logs not mounted or read-only — silently skip file logging.
        pass

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )


def read_recent_logs(n: int = 200, level_filter: str | None = None) -> list[dict]:
    """Read the last *n* structured log entries from the rotating log file.

    Used by GET /admin/logs in the dashboard.  Returns [] if the file does
    not exist (e.g. container started without the volume mount).
    """
    if not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        # Also check rotated files if we need more lines
        entries = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                entry = {"ts": "", "level": "RAW", "logger": "", "msg": line}
            if level_filter and entry.get("level", "").upper() != level_filter.upper():
                continue
            entries.append(entry)
            if len(entries) >= n:
                break
        return list(reversed(entries))
    except OSError:
        return []
