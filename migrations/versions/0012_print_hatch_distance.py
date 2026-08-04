"""Add print_records.hatch_distance_mm — the hatch distance this print was run at.

Revision ID: 0012_print_hatch_distance
Revises: 0011_plate_geometry_cache
Create Date: 2026-08-05

Hatch distance was only ever available per material, from the machine preset, so
every print of a given material was hatched at whatever that preset held. The
machine logs say that is wrong: ``*_Monitor100.log`` records the applied
parameter set on ``|P|`` lines, and its hatch field moved 0.16 -> 0.10 -> 0.90 mm
across steel jobs on this machine — a 9x span, while the preset claimed a fixed
0.12 mm for every one of them. Scan length is ~1/hatch, so that single number
dominates the whole time estimate.

This mirrors 0009_print_layer_thickness exactly: the parameter belongs to the
print, not to the machine. NULL means "not specified" — the estimate falls back
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
