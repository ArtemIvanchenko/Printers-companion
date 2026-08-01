"""Add plate_geometry_cache — content-addressed cache of LayerGeometrySeries.

Revision ID: 0011_plate_geometry_cache
Revises: 0010_index_session_classification
Create Date: 2026-08-01

Co-hatching a real plate (analytics.prediction.layer_engine.compute_layer_series)
is minutes of CPU, but its result depends only on the STL bodies' bytes and
hatch_distance_mm — material and layer_thickness_mm do not enter it (thickness
only shifts the sample points by half a layer via the boundary padding; measured
impact on summed hatch_mm is <=0.21% for a 2x thickness change, below the
already-accepted +-0.7% noise floor of the 90-level sampling grid). Re-estimating
a record after only changing material, or estimating a second record built from
the same STL files (a reprint), used to redo that work from scratch every time.

cache_key is a hash of the bodies' checksums in mesh order (part/support
tagged) plus hatch_distance_mm — see
analytics.prediction.plate_estimator._geometry_cache_key. It is NOT a foreign
key to any print record: the whole point is that unrelated records sharing the
same STL bytes hit the same row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_plate_geometry_cache"
down_revision = "0010_index_session_classification"
branch_labels = None
depends_on = None

_TABLE = "plate_geometry_cache"


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE in existing:
        return  # idempotent: safe to re-run against a database that already has it
    op.create_table(
        _TABLE,
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("series_json", sa.JSON(), nullable=False),
        sa.Column("body_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
    )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE in existing:
        op.drop_table(_TABLE)
