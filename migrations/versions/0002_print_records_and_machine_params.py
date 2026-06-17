"""Print archive: print_records, print_record_files, machine_params.

Revision ID: 0002_print_records_and_machine_params
Revises: 0001_initial_metadata
Create Date: 2026-06-12
"""

import sqlalchemy as sa
from alembic import op

from storage.db.base import Base

import domain.models.entities  # noqa: F401


revision = "0002_print_records_and_machine_params"
down_revision = "0001_initial_metadata"
branch_labels = None
depends_on = None

_TABLES = ("print_record_files", "print_records", "machine_params")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Pre-Alembic databases already have these tables — skip create_all to avoid
    # DDL lock contention on existing tables inside a PostgreSQL transaction.
    if all(inspector.has_table(t) for t in _TABLES):
        return
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    for name in _TABLES:
        table = Base.metadata.tables.get(name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
