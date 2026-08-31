"""Deduplicate attachments within one shared print card.

Revision ID: 0017_unique_print_file_content
Revises: 0016_operator_job_affinity
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_unique_print_file_content"
down_revision = "0016_operator_job_affinity"
branch_labels = None
depends_on = None

_INDEX = "ux_print_record_files_record_checksum"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("print_record_files"):
        return

    # Keep the oldest row for any duplicates created by concurrent uploads
    # before this migration. Their object bytes are identical by checksum, so
    # removing only redundant metadata cannot change the calculated plate.
    # Empty legacy checksums are unknown rather than identical and must remain.
    op.execute(sa.text("""
        DELETE FROM print_record_files
         WHERE file_id IN (
             SELECT file_id
               FROM (
                   SELECT file_id,
                          ROW_NUMBER() OVER (
                              PARTITION BY record_id, checksum
                              ORDER BY uploaded_at, file_id
                          ) AS duplicate_number
                     FROM print_record_files
               ) AS ranked_files
              WHERE duplicate_number > 1
                AND file_id IN (
                    SELECT file_id
                      FROM print_record_files
                     WHERE checksum <> ''
                )
         )
    """))
    indexes = {index["name"] for index in inspector.get_indexes("print_record_files")}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            "print_record_files",
            ["record_id", "checksum"],
            unique=True,
            postgresql_where=sa.text("checksum <> ''"),
            sqlite_where=sa.text("checksum <> ''"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("print_record_files"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("print_record_files")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="print_record_files")
