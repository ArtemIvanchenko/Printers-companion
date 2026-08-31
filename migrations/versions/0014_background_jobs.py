"""Add restart-safe background job queue.

Revision ID: 0014_background_jobs
Revises: 0013_print_record_concurrency
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_background_jobs"
down_revision = "0013_print_record_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("background_jobs"):
        return
    op.create_table(
        "background_jobs",
        sa.Column("job_id", sa.String(length=80), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_background_jobs_idempotency_key"),
    )
    op.create_index("ix_background_jobs_entity_id", "background_jobs", ["entity_id"])
    op.create_index("ix_background_jobs_lease_until", "background_jobs", ["lease_until"])
    op.create_index(
        "ix_background_jobs_claim",
        "background_jobs",
        ["job_type", "status", "available_at"],
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("background_jobs"):
        op.drop_table("background_jobs")
