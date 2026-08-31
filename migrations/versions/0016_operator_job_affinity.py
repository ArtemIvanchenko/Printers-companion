"""Pin background and import jobs to their originating operator PC.

Revision ID: 0016_operator_job_affinity
Revises: 0015_import_stability_attempts
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_operator_job_affinity"
down_revision = "0015_import_stability_attempts"
branch_labels = None
depends_on = None

_LEGACY_OWNER = "legacy-unassigned"


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("compute_node_registrations"):
        op.create_table(
            "compute_node_registrations",
            sa.Column("compute_node_id", sa.String(length=80), nullable=False),
            sa.Column("instance_id", sa.String(length=64), nullable=False),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("compute_node_id"),
            sa.UniqueConstraint(
                "instance_id",
                name="uq_compute_node_registrations_instance_id",
            ),
        )
    if inspector.has_table("background_jobs"):
        if "owner_node_id" not in _columns("background_jobs"):
            op.add_column(
                "background_jobs",
                sa.Column(
                    "owner_node_id",
                    sa.String(length=80),
                    nullable=False,
                    server_default=_LEGACY_OWNER,
                ),
            )
        if "lease_generation" not in _columns("background_jobs"):
            op.add_column(
                "background_jobs",
                sa.Column(
                    "lease_generation",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
        indexes = _indexes("background_jobs")
        if "ix_background_jobs_claim" in indexes:
            op.drop_index("ix_background_jobs_claim", table_name="background_jobs")
        op.create_index(
            "ix_background_jobs_claim",
            "background_jobs",
            ["owner_node_id", "job_type", "status", "available_at"],
        )

    if inspector.has_table("import_jobs"):
        columns = _columns("import_jobs")
        if "owner_node_id" not in columns:
            op.add_column(
                "import_jobs",
                sa.Column(
                    "owner_node_id",
                    sa.String(length=80),
                    nullable=False,
                    server_default=_LEGACY_OWNER,
                ),
            )
        if "print_record_id" not in columns:
            op.add_column(
                "import_jobs",
                sa.Column("print_record_id", sa.String(length=80), nullable=True),
            )
            op.create_index(
                "ix_import_jobs_print_record_id",
                "import_jobs",
                ["print_record_id"],
            )
        if "lease_owner" not in columns:
            op.add_column(
                "import_jobs",
                sa.Column("lease_owner", sa.String(length=160), nullable=True),
            )
        if "lease_until" not in columns:
            op.add_column(
                "import_jobs",
                sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            )
        if "lease_generation" not in columns:
            op.add_column(
                "import_jobs",
                sa.Column(
                    "lease_generation",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                ),
            )
        if "source_objects" not in columns:
            op.add_column(
                "import_jobs",
                sa.Column(
                    "source_objects",
                    sa.JSON(),
                    nullable=False,
                    server_default="{}",
                ),
            )
        if "ix_import_jobs_owner_claim" not in _indexes("import_jobs"):
            op.create_index(
                "ix_import_jobs_owner_claim",
                "import_jobs",
                ["owner_node_id", "status", "postponed_until", "lease_until"],
            )

    if inspector.has_table("notification_outbox"):
        if "owner_node_id" not in _columns("notification_outbox"):
            op.add_column(
                "notification_outbox",
                sa.Column(
                    "owner_node_id",
                    sa.String(length=80),
                    nullable=False,
                    server_default=_LEGACY_OWNER,
                ),
            )
        if "ix_notification_outbox_owner_pending" not in _indexes("notification_outbox"):
            op.create_index(
                "ix_notification_outbox_owner_pending",
                "notification_outbox",
                ["owner_node_id", "channel", "status", "created_at"],
            )

    # Ownership of pre-migration active work is unknowable. Failing it is safer
    # than silently running a local-path job on an arbitrary PC. Terminal audit
    # rows remain intact under the reserved read-only legacy identity.
    if inspector.has_table("background_jobs"):
        op.execute(sa.text("""
            UPDATE background_jobs
               SET status = 'failed',
                   error = COALESCE(
                       error,
                       'Upgrade stopped unowned work; explicitly create a fresh local job'
                   ),
                   lease_owner = NULL,
                   lease_until = NULL,
                   finished_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE owner_node_id = 'legacy-unassigned'
               AND status IN ('pending', 'running')
        """))
    if inspector.has_table("import_jobs"):
        op.execute(sa.text("""
            UPDATE import_jobs
               SET status = 'failed',
                   error = COALESCE(
                       error,
                       'Upgrade stopped unowned import; re-upload it on the intended operator PC'
                   ),
                   lease_owner = NULL,
                   lease_until = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE owner_node_id = 'legacy-unassigned'
               AND status NOT IN ('done', 'failed', 'ignored', 'needs_operator_context')
        """))
    if inspector.has_table("notification_outbox"):
        op.execute(sa.text("""
            UPDATE notification_outbox
               SET status = 'failed',
                   error = COALESCE(error, 'Legacy notification has no operator-PC owner')
             WHERE owner_node_id = 'legacy-unassigned'
               AND status = 'pending'
        """))

    # PostgreSQL is the production NAS backend. Remove the temporary backfill
    # defaults there so future code cannot accidentally insert an unowned row.
    # SQLite cannot ALTER DEFAULT in-place; it is used only for local tests.
    if op.get_bind().dialect.name != "sqlite":
        if inspector.has_table("background_jobs"):
            op.alter_column(
                "background_jobs",
                "owner_node_id",
                existing_type=sa.String(length=80),
                server_default=None,
            )
        if inspector.has_table("import_jobs"):
            op.alter_column(
                "import_jobs",
                "owner_node_id",
                existing_type=sa.String(length=80),
                server_default=None,
            )
        if inspector.has_table("notification_outbox"):
            op.alter_column(
                "notification_outbox",
                "owner_node_id",
                existing_type=sa.String(length=80),
                server_default=None,
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("compute_node_registrations"):
        op.drop_table("compute_node_registrations")

    if inspector.has_table("notification_outbox"):
        if "ix_notification_outbox_owner_pending" in _indexes("notification_outbox"):
            op.drop_index(
                "ix_notification_outbox_owner_pending",
                table_name="notification_outbox",
            )
        if "owner_node_id" in _columns("notification_outbox"):
            op.drop_column("notification_outbox", "owner_node_id")

    if inspector.has_table("import_jobs"):
        columns = _columns("import_jobs")
        if "ix_import_jobs_owner_claim" in _indexes("import_jobs"):
            op.drop_index("ix_import_jobs_owner_claim", table_name="import_jobs")
        if "ix_import_jobs_print_record_id" in _indexes("import_jobs"):
            op.drop_index("ix_import_jobs_print_record_id", table_name="import_jobs")
        for column in (
            "source_objects",
            "lease_generation",
            "lease_until",
            "lease_owner",
            "print_record_id",
            "owner_node_id",
        ):
            if column in columns:
                op.drop_column("import_jobs", column)

    if inspector.has_table("background_jobs"):
        if "ix_background_jobs_claim" in _indexes("background_jobs"):
            op.drop_index("ix_background_jobs_claim", table_name="background_jobs")
        if "owner_node_id" in _columns("background_jobs"):
            op.drop_column("background_jobs", "owner_node_id")
        if "lease_generation" in _columns("background_jobs"):
            op.drop_column("background_jobs", "lease_generation")
        op.create_index(
            "ix_background_jobs_claim",
            "background_jobs",
            ["job_type", "status", "available_at"],
        )
