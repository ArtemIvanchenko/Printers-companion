"""Programmatic Alembic migrations — bring the database to head.

Single source of truth for schema is Alembic. This runs ``alembic upgrade
head`` from code so the schema auto-updates:

* standalone docker — once in the API entrypoint before workers spawn;
* shared NAS — explicit one-shot ``migrate`` service from exactly one PC;
  every production component then performs only a read-only head check.
* local dev — from the app lifespan (single process).

``create_all()`` is reserved for the test harness only, never runtime.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from core.config.settings import get_settings

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _ROOT / "alembic.ini"


def upgrade_to_head() -> None:
    """Run ``alembic upgrade head`` against the configured database."""
    cfg = Config(str(_ALEMBIC_INI))
    # Absolute paths so it works regardless of the process CWD.
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")
    logger.info("alembic: database upgraded to head")


def assert_schema_at_head() -> None:
    """Read-only production guard against running code on an old NAS schema."""
    from storage.db.session import engine

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    expected = set(ScriptDirectory.from_config(cfg).get_heads())
    with engine.connect() as connection:
        installed = set(MigrationContext.configure(connection).get_current_heads())
    if installed != expected:
        raise RuntimeError(
            "Database schema is not at the application head: "
            f"installed={sorted(installed) or ['<none>']}, expected={sorted(expected)}. "
            "Stop operator services and run the one-shot 'migrate' service from exactly one PC."
        )
    logger.info("alembic: schema revision verified at %s", ", ".join(sorted(expected)))
