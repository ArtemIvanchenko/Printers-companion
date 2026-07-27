"""Make machine_params.time_correction_by_mat NOT NULL, matching the model.

Revision ID: 0006_time_correction_not_null
Revises: 0005_per_material_correction
Create Date: 2026-07-27

0005 added the column as nullable, but ``MachineParams.time_correction_by_mat``
is a non-optional ``Mapped[dict]`` — so every database that reached its current
schema *through the migration chain* (i.e. every operator machine) has a
nullable column where the ORM guarantees a value. Databases created fresh got
NOT NULL, so the two diverged silently.

Found by scripts/check_migration_drift.py once the baseline stopped being
generated from the live models.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_time_correction_not_null"
down_revision = "0005_per_material_correction"
branch_labels = None
depends_on = None

_TABLE = "machine_params"
_COLUMN = "time_correction_by_mat"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"]: c for c in sa.inspect(bind).get_columns(_TABLE)}
    column = columns.get(_COLUMN)
    if column is None or not column.get("nullable", False):
        return  # fresh databases already have it NOT NULL

    # Backfill before tightening: existing rows may hold NULL.
    op.execute(sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = '{{}}' WHERE {_COLUMN} IS NULL"))

    if bind.dialect.name == "sqlite":
        # SQLite cannot ALTER COLUMN; the table has to be rebuilt. recreate is
        # forced because "auto" left the nullability unchanged here.
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.alter_column(
                _COLUMN, existing_type=sa.JSON(), nullable=False,
                server_default=sa.text("'{}'"),
            )
    else:
        op.alter_column(
            _TABLE, _COLUMN, existing_type=sa.JSON(), nullable=False,
            server_default=sa.text("'{}'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.alter_column(_COLUMN, existing_type=sa.JSON(), nullable=True)
    else:
        op.alter_column(_TABLE, _COLUMN, existing_type=sa.JSON(), nullable=True)
