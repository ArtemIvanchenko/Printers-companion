"""Add configurable galvo jump scanner params to machine_params.

Adds ``jump_speed_mm_s`` and ``jump_delay_ms`` — used by the PySLM vector
build-time estimate. Written with explicit ``op.add_column`` DDL (not
``create_all``) so existing databases actually receive the new columns via
``ALTER TABLE``. Idempotent: skips a column that already exists, because the
``create_all`` baseline in 0001/0002 already builds it on freshly-created DBs.

Revision ID: 0003_scanner_jump_params
Revises: 0002_print_records_and_machine_params
Create Date: 2026-06-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_scanner_jump_params"
down_revision = "0002_print_records_and_machine_params"
branch_labels = None
depends_on = None

_TABLE = "machine_params"
_NEW_COLUMNS = ("jump_speed_mm_s", "jump_delay_ms")


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    existing = _existing_columns()
    for name in _NEW_COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    for name in reversed(_NEW_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
