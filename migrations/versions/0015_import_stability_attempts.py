"""Persist import stability retry count across worker restarts.

Revision ID: 0015_import_stability_attempts
Revises: 0014_background_jobs
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_import_stability_attempts"
down_revision = "0014_background_jobs"
branch_labels = None
depends_on = None

_TABLE = "import_jobs"
_COLUMN = "stability_check_attempts"


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
