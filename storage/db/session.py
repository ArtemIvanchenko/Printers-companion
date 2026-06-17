from collections.abc import Generator
from contextlib import contextmanager
import logging

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from core.config.settings import get_settings

logger = logging.getLogger(__name__)


def _json_default_dict() -> dict:
    return {}


def _json_default_list() -> list:
    return []


def _connect_args(url: str) -> dict[str, object]:
    if url.startswith("sqlite"):
        return {"check_same_thread": False, "timeout": 30}
    # For PostgreSQL/MySQL - use TCP_KEEPALIVES instead
    return {
        "connect_timeout": 30,  # TCP connection timeout (PostgreSQL supports this)
    }


settings = get_settings()

pool_config = {
    "pool_pre_ping": True,
    "connect_args": _connect_args(settings.database_url),
}

if not settings.database_url.startswith("sqlite"):
    pool_config.update({
        "poolclass": QueuePool,
        "pool_size": 15,  # Increased from 10
        "max_overflow": 30,  # Increased from 20
        "pool_recycle": 3600,  # Recycle connections every hour
        "pool_timeout": 30,  # Wait 30s for a connection from the pool
    })
else:
    # SQLite uses NullPool to avoid connection issues
    pool_config.update({
        "poolclass": NullPool,
    })

engine = create_engine(settings.database_url, **pool_config, echo_pool=(settings.app_env == "local"))
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    """Log successful connections for debugging."""
    if settings.app_env == "local":
        logger.debug("Database connection established")


@event.listens_for(engine, "checkout")
def receive_checkout(dbapi_conn, connection_record, connection_proxy):
    """Log connection checkout events."""
    if settings.app_env == "local":
        logger.debug("Database connection checked out from pool")

def get_db() -> Generator[Session, None, None]:
    """Request-scoped session — the unit-of-work boundary.

    Commits once if the handler succeeds, rolls back on any exception. Route
    handlers and repositories must NOT commit themselves (repos only flush).
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Session for non-request code (background tasks, workers, startup).

    Same unit-of-work semantics as :func:`get_db`: commit on success, rollback
    on exception, always close. Use instead of a bare ``SessionLocal()`` +
    manual ``commit()``.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

