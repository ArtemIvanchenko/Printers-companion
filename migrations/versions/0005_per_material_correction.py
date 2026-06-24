"""Add per-material time-correction storage to machine_params.

Adds two columns used by the auto-calibration loop:

  • ``time_correction_by_mat`` (JSON) — {"steel": 1.15, "aluminum": 1.08, ...},
    per-material median(actual/predicted) factors learned from history.
  • ``correction_locked`` (bool) — when the operator pins factors manually,
    auto-calibration must not overwrite them.

The existing global ``time_correction_factor`` stays as a fallback for
materials with no per-material factor yet.

Revision ID: 0005_per_material_correction
Revises: 0004_machine_presets
Create Date: 2026-06-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_per_material_correction"
down_revision = "0004_machine_presets"
branch_labels = None
depends_on = None

_TABLE = "machine_params"


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if "time_correction_by_mat" not in cols:
        op.add_column(_TABLE, sa.Column("time_correction_by_mat", sa.JSON(), nullable=True))
    if "correction_locked" not in cols:
        op.add_column(
            _TABLE,
            sa.Column("correction_locked", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if "correction_locked" in cols:
        op.drop_column(_TABLE, "correction_locked")
    if "time_correction_by_mat" in cols:
        op.drop_column(_TABLE, "time_correction_by_mat")
