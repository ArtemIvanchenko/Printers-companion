"""Separate bounded worker failures from file-stability retries.

Revision ID: 0022_import_execution_attempts
Revises: 0021_layer_cycle_models
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_import_execution_attempts"
down_revision = "0021_layer_cycle_models"
branch_labels = None
depends_on = None

_TABLE = "import_jobs"
_COLUMN = "execution_attempts"


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN not in columns:
        # Historical stability counters mixed unrelated failure categories.
        # Preserve them; neither audit JSON nor lease generations can recover
        # a trustworthy historical execution-failure count.
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
