"""Programmatic Alembic migrations — bring the database to head.

Single source of truth for schema is Alembic. This runs ``alembic upgrade
head`` from code so the schema auto-updates:

* prod/docker — once in the container entrypoint before workers spawn
  (``alembic upgrade head && uvicorn …``); race-free.
* local dev — from the app lifespan (single process).

``create_all()`` is reserved for the test harness only, never runtime.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

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
