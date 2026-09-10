"""Add print_records.hatch_distance_mm — the hatch distance this print was run at.

Revision ID: 0012_print_hatch_distance
Revises: 0011_plate_geometry_cache
Create Date: 2026-08-05

Hatch distance was only available per material, from the machine preset, so
every print of a given material was estimated as if it shared one value. It is
actually a slicer/process-strategy parameter and may vary per job. Scan length
scales approximately as 1/hatch, so the value materially affects time.

The firmware schema of Monitor100 ``|P|`` records is not documented in this
project. Their unlabelled numeric positions must not be treated as hatch
distance until independently decoded and verified against a known job export.

This mirrors 0009_print_layer_thickness: the effective parameter belongs to the
job strategy, not only to the machine. NULL means "not specified" — the estimate falls back
to the preset/machine value, so existing records keep behaving as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_print_hatch_distance"
down_revision = "0011_plate_geometry_cache"
branch_labels = None
depends_on = None

_TABLE = "print_records"
_COLUMN = "hatch_distance_mm"


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        return  # idempotent: safe to re-run against a DB that already has it
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
