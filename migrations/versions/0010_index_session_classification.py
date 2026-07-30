"""Index sessions.classification — it became a filter column.

Revision ID: 0010_index_session_classification
Revises: 0009_print_layer_thickness
Create Date: 2026-07-30

The column existed since 0001 but was never written (creation passed no value,
and save_session_payload only assigned context/timestamps), so every row held
the INCOMPLETE_OR_UNKNOWN default and nothing could usefully filter on it.
Now that it tracks the payload, `_load_sessions` in api/routes/analysis.py
selects real prints by it — and cross-session analysis, the maintenance
forecast and every predicted-vs-actual pair run through that filter.

Harmless at eleven rows; the point is that it stays a lookup rather than a
sequential scan as prints accumulate.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_index_session_classification"
down_revision = "0009_print_layer_thickness"
branch_labels = None
depends_on = None

_TABLE = "sessions"
_COLUMN = "classification"
_INDEX = "ix_sessions_classification"


def upgrade() -> None:
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX in existing:
        return  # idempotent: safe to re-run
    op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX in existing:
        op.drop_index(_INDEX, table_name=_TABLE)
