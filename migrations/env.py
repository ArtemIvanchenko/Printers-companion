from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text

from core.config.settings import get_settings
from storage.db.base import Base

import domain.models.entities  # noqa: F401


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata

# Alembic hardcodes varchar(32) for alembic_version.version_num (ddl/impl.py:173).
# Our revision IDs are up to 41 chars, so we must ensure the column is wide enough
# before any migration runs — otherwise the first INSERT fails.
_WIDEN_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'alembic_version'
                 AND column_name = 'version_num'
                 AND character_maximum_length < 64) THEN
        ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);
    END IF;
END $$;
"""


def _ensure_version_col(connection) -> None:
    """Widen version_num to VARCHAR(64) on existing DBs; create the table with
    the right size on fresh ones so Alembic never touches a VARCHAR(32) column."""
    if not inspect(connection).has_table("alembic_version"):
        connection.execute(text(
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(64) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
        connection.commit()
        return

    # _WIDEN_SQL is a PL/pgSQL DO block. Running it on any other backend raises,
    # which made every alembic command fail once alembic_version existed —
    # including the second `upgrade head` on the SQLite setup the README
    # documents for running tests without Docker. SQLite has no VARCHAR length
    # enforcement, so there is nothing to widen there anyway.
    if connection.dialect.name != "postgresql":
        return

    connection.execute(text(_WIDEN_SQL))
    connection.commit()


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _ensure_version_col(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLite's Alembic implementation reports non-transactional DDL. A
        # migration that mixes ALTER TABLE with legacy-row backfills can leave
        # its DML and even the alembic_version bump in an implicit transaction;
        # closing the connection then rolls them back while the DDL remains.
        # An explicit commit is harmless after PostgreSQL's transactional block
        # and makes local upgrade tests genuinely atomic from Alembic's view.
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
