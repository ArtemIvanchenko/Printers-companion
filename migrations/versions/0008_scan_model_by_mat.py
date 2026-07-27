"""Add machine_params.scan_model_by_mat — fitted per-(material, layer-thickness)
scan-time models calibrated from real per-layer burn_ms printer logs.

Revision ID: 0008_scan_model_by_mat
Revises: 0007_recoat_time_by_mat
Create Date: 2026-07-28

Each entry is keyed "material@thickness" (e.g. "steel@0.060") and stores the
positional beta vector over analytics.prediction.layer_engine.GEOMETRY_FEATURES
plus an intercept, with fit metadata (r2, n_layers, source sessions). The
coefficients are NOT physical speeds — real-data validation showed the geometry
components are collinear, so only the fitted linear map is identifiable; that is
also why a model is only ever applied to its exact mode key.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_scan_model_by_mat"
down_revision = "0007_recoat_time_by_mat"
branch_labels = None
depends_on = None

_TABLE = "machine_params"
_COLUMN = "scan_model_by_mat"


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
