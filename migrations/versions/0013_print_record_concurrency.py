"""Protect shared print cards from concurrent workstation edits.

Revision ID: 0013_print_record_concurrency
Revises: 0012_print_hatch_distance
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_print_record_concurrency"
down_revision = "0012_print_hatch_distance"
branch_labels = None
depends_on = None

_TABLE = "print_records"


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if "revision" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        )
    if "updated_by" not in columns:
        op.add_column(_TABLE, sa.Column("updated_by", sa.String(length=120), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if "updated_by" in columns:
        op.drop_column(_TABLE, "updated_by")
    if "revision" in columns:
        op.drop_column(_TABLE, "revision")
