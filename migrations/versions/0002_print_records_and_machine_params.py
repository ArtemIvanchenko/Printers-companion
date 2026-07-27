"""Print archive: print_records, print_record_files, machine_params.

Revision ID: 0002_print_records_and_machine_params
Revises: 0001_initial_metadata
Create Date: 2026-06-12

This migration used to call ``Base.metadata.create_all()``, which created every
table in the ORM models — not the three it names — and made the schema a
database receives depend on the code version rather than on the migration
chain. 0001 now carries an explicit frozen snapshot that already includes these
three tables, so on a fresh database this migration finds nothing to do.

It still matters for databases that predate Alembic: 0001 skips itself when
``sessions`` already exists, so such a database arrives here without the print
archive. Each table is therefore created only when missing.

The columns below are the ones that existed *at this point in history*:
jump_speed_mm_s / jump_delay_ms arrive in 0003 and time_correction_by_mat /
correction_locked in 0005. Do not add later columns here.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_print_records_and_machine_params"
down_revision = "0001_initial_metadata"
branch_labels = None
depends_on = None

_TABLES = ("print_records", "print_record_files", "machine_params")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "print_records" not in existing:
        op.create_table(
            "print_records",
            sa.Column("record_id", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=240), nullable=False),
            sa.Column("material", sa.String(length=120), nullable=False),
            sa.Column("session_id", sa.String(length=80), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("printed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("powder_cost_rub_per_kg", sa.Float(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
            sa.PrimaryKeyConstraint("record_id"),
        )
        op.create_index(op.f("ix_print_records_created_at"), "print_records", ["created_at"])
        op.create_index(op.f("ix_print_records_printed_at"), "print_records", ["printed_at"])
        op.create_index(op.f("ix_print_records_session_id"), "print_records", ["session_id"])

    if "print_record_files" not in existing:
        op.create_table(
            "print_record_files",
            sa.Column("file_id", sa.String(length=80), nullable=False),
            sa.Column("record_id", sa.String(length=80), nullable=False),
            sa.Column("object_uri", sa.String(length=700), nullable=False),
            sa.Column("file_name", sa.String(length=300), nullable=False),
            sa.Column("file_type", sa.String(length=40), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("checksum", sa.String(length=128), nullable=False),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["record_id"], ["print_records.record_id"]),
            sa.PrimaryKeyConstraint("file_id"),
        )
        op.create_index(op.f("ix_print_record_files_checksum"), "print_record_files", ["checksum"])
        op.create_index(op.f("ix_print_record_files_file_type"), "print_record_files", ["file_type"])
        op.create_index(op.f("ix_print_record_files_record_id"), "print_record_files", ["record_id"])

    if "machine_params" not in existing:
        op.create_table(
            "machine_params",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("hatch_speed_mm_s", sa.Float(), nullable=True),
            sa.Column("contour_speed_mm_s", sa.Float(), nullable=True),
            sa.Column("hatch_distance_mm", sa.Float(), nullable=True),
            sa.Column("time_correction_factor", sa.Float(), nullable=True),
            sa.Column("layer_thickness_mm", sa.Float(), nullable=True),
            sa.Column("laser_count", sa.Integer(), nullable=True),
            sa.Column("recoat_time_ms", sa.Float(), nullable=True),
            sa.Column("powder_cost_rub_per_kg", sa.Float(), nullable=True),
            sa.Column("gas_cost_rub_per_atm", sa.Float(), nullable=True),
            sa.Column("gas_atm_per_print", sa.Float(), nullable=True),
            sa.Column("filter_cost_rub", sa.Float(), nullable=True),
            sa.Column("filter_lifetime_hours", sa.Float(), nullable=True),
            sa.Column("platform_cost_rub", sa.Float(), nullable=True),
            sa.Column("material_densities", sa.JSON(), nullable=False),
            sa.Column("hatch_speeds_by_mat", sa.JSON(), nullable=False),
            sa.Column("build_area_cm2", sa.Float(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name in reversed(_TABLES):
        if name in existing:
            op.drop_table(name)
