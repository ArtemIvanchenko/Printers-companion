"""Read-only API dependency readiness, not a claim that workers are alive.

No migrations, probe uploads, queue mutations or long-lived pools are created.
Each network operation has a short timeout; errors do not expose credentials.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from minio import Minio
import redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
import urllib3

from core.config.settings import Settings
from core.versioning.provenance import build_manifest
from core.versioning.version import APP_VERSION

logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[1]
_TIMEOUT = 2


def _database_checks(settings: Settings) -> dict[str, bool]:
    checks = {"database": False, "schema": False}
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and (
        not url.database or url.database == ":memory:" or not Path(url.database).is_file()
    ):
        # A readiness GET must not initialize an absent SQLite database.
        return checks
    connect_args = (
        {"connect_timeout": _TIMEOUT,
         "options": "-c statement_timeout=2000 -c default_transaction_read_only=on"}
        if settings.database_url.startswith("postgresql+psycopg://")
        else {"timeout": _TIMEOUT} if settings.database_url.startswith("sqlite") else {}
    )
    engine = create_engine(settings.database_url, poolclass=NullPool, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.execute(text("SELECT 1"))
            checks["database"] = True
            config = Config(str(_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(_ROOT / "migrations"))
            expected = set(ScriptDirectory.from_config(config).get_heads())
            installed = set(MigrationContext.configure(connection).get_current_heads())
            checks["schema"] = bool(expected) and installed == expected
    except Exception:
        logger.warning("readiness: database or schema unavailable")
    finally:
        engine.dispose()
    return checks


def _redis_ready(settings: Settings) -> bool:
    with redis.from_url(settings.redis_url, socket_connect_timeout=_TIMEOUT,
                        socket_timeout=_TIMEOUT) as client:
        return bool(client.ping())


def _object_store_checks(settings: Settings) -> dict[str, bool]:
    pool = urllib3.PoolManager(timeout=urllib3.Timeout(connect=_TIMEOUT, read=_TIMEOUT), retries=False)
    try:
        client = Minio(settings.minio_endpoint, access_key=settings.minio_root_user,
                       secret_key=settings.minio_root_password, secure=settings.minio_secure,
                       http_client=pool)
        # One authenticated request proves read access and enumerates all required
        # buckets. It does NOT prove object write/delete permission or free space.
        buckets = {bucket.name for bucket in client.list_buckets()}
        required = {settings.minio_bucket_raw, settings.minio_bucket_reports,
                    settings.minio_bucket_stls, settings.minio_bucket_magics,
                    settings.minio_bucket_photos, settings.minio_bucket_docs}
        return {"minio": True, "minio_buckets": required <= buckets}
    finally:
        pool.clear()


def check_api_readiness(settings: Settings) -> dict:
    """Additive response contract: ``checks`` stays a mapping of booleans."""
    checks = {"database": False, "schema": False, "redis": False,
              "minio": False, "minio_buckets": False}
    try:
        checks.update(_database_checks(settings))
    except Exception:
        logger.warning("readiness: invalid database configuration")
    try:
        checks["redis"] = _redis_ready(settings)
    except Exception:
        logger.warning("readiness: redis unavailable")
    try:
        checks.update(_object_store_checks(settings))
    except Exception:
        logger.warning("readiness: object store unavailable")
    return {
        "status": "ready" if all(checks.values()) else "not_ready",
        "scope": "api_dependencies",
        "checks": checks,
        "version": APP_VERSION,
        "build": build_manifest(),
        "capabilities": {
            "storage_write": {"status": "unknown", "reason": "read_only_probe"},
            "import_worker": {"status": "unknown", "reason": "no_worker_heartbeat"},
            "estimation_worker": {"status": "unknown", "reason": "no_worker_heartbeat"},
            "nas_sync_worker": {"status": "unknown", "reason": "no_worker_heartbeat"},
        },
    }
