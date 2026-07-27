#!/usr/bin/env python
"""Fail if the Alembic migrations do not reproduce the ORM models.

The test harness builds its schema with ``create_all()`` straight from the
models (tests/conftest.py), so migrations are never exercised by the suite: a
column added to a model without a matching migration passes every test and then
breaks on a real upgrade. Run this against a database already migrated to head.

Usage:
    DATABASE_URL=postgresql+psycopg://... alembic upgrade head
    DATABASE_URL=postgresql+psycopg://... python scripts/check_migration_drift.py
"""
from __future__ import annotations

import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from core.config.settings import get_settings

# Importing the aggregate model module registers every table on Base.metadata.
from domain.models import entities  # noqa: F401
from storage.db.base import Base

# Tables Alembic must ignore: its own bookkeeping, plus anything created
# outside the migration chain.
_IGNORED_TABLES = {"alembic_version"}


def main() -> int:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_schemas": False},
        )
        diff = compare_metadata(context, Base.metadata)

    def _table_of(entry) -> str | None:
        # Entries are ("add_table", Table) or ("add_column", schema, table, col), …
        if isinstance(entry, tuple) and len(entry) >= 2:
            second = entry[1]
            return getattr(second, "name", entry[2] if len(entry) > 2 else None)
        return None

    diff = [d for d in diff if _table_of(d) not in _IGNORED_TABLES]

    if not diff:
        print("OK: migrations reproduce the ORM models")
        return 0

    print("Schema drift between the ORM models and the migration chain:\n")
    for entry in diff:
        print(f"  {entry}")
    print(
        "\nAdd a migration under migrations/versions/ that applies these changes "
        "(or correct the model if the migration is right)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
