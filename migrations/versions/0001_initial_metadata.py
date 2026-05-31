"""Initial metadata tables.

Revision ID: 0001_initial_metadata
Revises:
Create Date: 2026-04-27
"""

from alembic import op

from storage.db.base import Base

import domain.models.entities  # noqa: F401


revision = "0001_initial_metadata"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())

