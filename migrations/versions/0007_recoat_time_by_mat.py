"""Add machine_params.recoat_time_by_mat — per-material recoat time, calibrated
from real per-layer pour_ms readings in printer logs.

Revision ID: 0007_recoat_time_by_mat
Revises: 0006_time_correction_not_null
Create Date: 2026-07-28

Mirrors ``time_correction_by_mat`` (0005): a JSON map material -> value,
auto-calibrated by ``analytics.prediction.recoat_calibration`` and gated by the
same ``correction_locked`` flag. Unlike scan-time correction, this is not a
multiplier on an estimate — it is the recoat duration itself (ms), replacing
the flat ``recoat_time_ms`` / the hardcoded 9500 ms fallback for materials with
enough calibration history.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_recoat_time_by_mat"
down_revision = "0006_time_correction_not_null"
branch_labels = None
depends_on = None

_TABLE = "machine_params"
_COLUMN = "recoat_time_by_mat"


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        return  # idempotent: safe to re-run against a DB that already has it
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
