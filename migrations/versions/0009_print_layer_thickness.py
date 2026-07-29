"""Add print_records.layer_thickness_mm — the layer thickness this print was run at.

Revision ID: 0009_print_layer_thickness
Revises: 0008_scan_model_by_mat
Create Date: 2026-07-30

Layer thickness was only ever available globally, from machine_params, so every
print was costed and predicted at whatever the machine happened to be set to
last. It belongs to the print: the shop runs different thicknesses for different
jobs, and the fitted scan-time models are keyed "material@thickness" precisely
because a model does not transfer across thicknesses (validation showed R² < 0
when applied to a different one).

NULL means "not specified" — the estimate falls back to the machine default, so
existing records keep behaving exactly as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_print_layer_thickness"
down_revision = "0008_scan_model_by_mat"
branch_labels = None
depends_on = None

_TABLE = "print_records"
_COLUMN = "layer_thickness_mm"


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        return  # idempotent: safe to re-run against a DB that already has it
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
