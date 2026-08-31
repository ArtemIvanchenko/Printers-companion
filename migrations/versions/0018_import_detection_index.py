"""Index owner-local import duplicate detection.

Revision ID: 0018_import_detection_index
Revises: 0017_unique_print_file_content
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_import_detection_index"
down_revision = "0017_unique_print_file_content"
branch_labels = None
depends_on = None

_INDEX = "ix_import_jobs_owner_source_name_status"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_jobs"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("import_jobs")}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            "import_jobs",
            ["owner_node_id", "source_name", "status"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("import_jobs"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("import_jobs")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="import_jobs")
