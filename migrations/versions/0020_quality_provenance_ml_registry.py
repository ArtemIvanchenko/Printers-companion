"""Add strict quality labels and guarded ML model registry.

Revision ID: 0020_quality_provenance_ml_registry
Revises: 0019_domain_compute_affinity
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_quality_provenance_ml_registry"
down_revision = "0019_domain_compute_affinity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("quality_outcomes"):
        columns = {column["name"] for column in inspector.get_columns("quality_outcomes")}
        foreign_keys = {fk.get("name") for fk in inspector.get_foreign_keys("quality_outcomes")}
        with op.batch_alter_table("quality_outcomes") as batch:
            if "print_record_id" not in columns:
                batch.add_column(sa.Column("print_record_id", sa.String(length=80), nullable=True))
            if "inspection_result" not in columns:
                batch.add_column(sa.Column("inspection_result", sa.Text(), nullable=True))
            if "is_final" not in columns:
                batch.add_column(
                    sa.Column(
                        "is_final",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.false(),
                    )
                )
            if "supersedes_outcome_id" not in columns:
                batch.add_column(
                    sa.Column("supersedes_outcome_id", sa.String(length=80), nullable=True)
                )
            if "fk_quality_outcomes_print_record_id" not in foreign_keys:
                batch.create_foreign_key(
                    "fk_quality_outcomes_print_record_id",
                    "print_records",
                    ["print_record_id"],
                    ["record_id"],
                    ondelete="SET NULL",
                )
            if "fk_quality_outcomes_supersedes_outcome_id" not in foreign_keys:
                batch.create_foreign_key(
                    "fk_quality_outcomes_supersedes_outcome_id",
                    "quality_outcomes",
                    ["supersedes_outcome_id"],
                    ["outcome_id"],
                    ondelete="SET NULL",
                )

        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("quality_outcomes")}
        for name, columns_ in (
            ("ix_quality_outcomes_print_record_id", ["print_record_id"]),
            ("ix_quality_outcomes_is_final", ["is_final"]),
            ("ix_quality_outcomes_supersedes_outcome_id", ["supersedes_outcome_id"]),
        ):
            if name not in indexes:
                op.create_index(name, "quality_outcomes", columns_)

    if not inspector.has_table("ml_model_versions"):
        op.create_table(
            "ml_model_versions",
            sa.Column("model_version_id", sa.String(length=80), nullable=False),
            sa.Column("model_name", sa.String(length=120), nullable=False),
            sa.Column("algorithm", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("owner_node_id", sa.String(length=80), nullable=False),
            sa.Column("training_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("feature_schema_hash", sa.String(length=64), nullable=False),
            sa.Column("training_session_ids", sa.JSON(), nullable=False),
            sa.Column("training_size", sa.Integer(), nullable=False),
            sa.Column("positive_count", sa.Integer(), nullable=False),
            sa.Column("artifact_json", sa.JSON(), nullable=False),
            sa.Column("metrics_json", sa.JSON(), nullable=False),
            sa.Column("quality_gates_json", sa.JSON(), nullable=False),
            sa.Column("shadow_metrics_json", sa.JSON(), nullable=False),
            sa.Column("app_version", sa.String(length=80), nullable=False),
            sa.Column("analysis_version", sa.String(length=80), nullable=False),
            sa.Column("git_sha", sa.String(length=80), nullable=True),
            sa.Column("config_hash", sa.String(length=64), nullable=True),
            sa.Column("parent_model_version_id", sa.String(length=80), nullable=True),
            sa.Column("decision_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("model_version_id"),
            sa.UniqueConstraint(
                "model_name",
                "training_fingerprint",
                name="uq_ml_model_versions_name_training_fingerprint",
            ),
        )
        op.create_index(
            "ix_ml_model_versions_owner_node_id",
            "ml_model_versions",
            ["owner_node_id"],
        )
        op.create_index(
            "ix_ml_model_versions_name_status",
            "ml_model_versions",
            ["model_name", "status"],
        )
        op.create_index(
            "ux_ml_model_versions_one_active",
            "ml_model_versions",
            ["model_name"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
        op.create_index(
            "ux_ml_model_versions_one_shadow",
            "ml_model_versions",
            ["model_name"],
            unique=True,
            postgresql_where=sa.text("status = 'shadow'"),
            sqlite_where=sa.text("status = 'shadow'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("ml_model_versions"):
        op.drop_table("ml_model_versions")

    if not inspector.has_table("quality_outcomes"):
        return
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("quality_outcomes")}
    for name in (
        "ix_quality_outcomes_supersedes_outcome_id",
        "ix_quality_outcomes_is_final",
        "ix_quality_outcomes_print_record_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name="quality_outcomes")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("quality_outcomes")}
    foreign_keys = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("quality_outcomes")}
    with op.batch_alter_table("quality_outcomes") as batch:
        if "fk_quality_outcomes_supersedes_outcome_id" in foreign_keys:
            batch.drop_constraint("fk_quality_outcomes_supersedes_outcome_id", type_="foreignkey")
        if "fk_quality_outcomes_print_record_id" in foreign_keys:
            batch.drop_constraint("fk_quality_outcomes_print_record_id", type_="foreignkey")
        for name in ("supersedes_outcome_id", "is_final", "inspection_result", "print_record_id"):
            if name in columns:
                batch.drop_column(name)
