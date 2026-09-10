"""Store normal layer-cycle models independently from scan geometry models.

Revision ID: 0021_layer_cycle_models
Revises: 0020_quality_provenance_ml_registry
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_layer_cycle_models"
down_revision = "0020_quality_provenance_ml_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("machine_params"):
        return
    columns = {column["name"] for column in inspector.get_columns("machine_params")}
    if "layer_cycle_model_by_mode" not in columns:
        with op.batch_alter_table("machine_params") as batch:
            batch.add_column(sa.Column(
                "layer_cycle_model_by_mode",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("machine_params"):
        return
    columns = {column["name"] for column in inspector.get_columns("machine_params")}
    if "layer_cycle_model_by_mode" in columns:
        with op.batch_alter_table("machine_params") as batch:
            batch.drop_column("layer_cycle_model_by_mode")
