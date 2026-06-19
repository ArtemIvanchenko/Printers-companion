"""Add machine_presets table and seed known M-350 presets.

Creates the ``machine_presets`` table (one row per material/mode) and inserts
verified data for the two standard M-350 presets:

  • РС-300 СТД 60мкм (Aluminum) — parameters verified from LaserStudio
    screenshots (2026-06-17).
  • 12Х18Н10Т 32мкм (Steel) — approximate values; exact preset not yet
    photographed.

Steel density (7.9 g/cm³) is intentionally NOT updated here — DML on live
application tables inside an Alembic migration causes lock contention with the
running API. Set it via PUT /settings/machine instead.

Revision ID: 0004_machine_presets
Revises: 0003_scanner_jump_params
Create Date: 2026-06-19
"""

import sqlalchemy as sa
from alembic import op

from storage.db.base import Base

import domain.models.entities  # noqa: F401

revision = "0004_machine_presets"
down_revision = "0003_scanner_jump_params"
branch_labels = None
depends_on = None

_TABLE = "machine_presets"

_SEED_PRESETS = [
    {
        "name": "РС-300 СТД 60мкм (Алюминий)",
        "material": "aluminum",
        "layer_thickness_mm": 0.06,
        "hatch_speed_mm_s": 1528.0,
        "contour_speed_mm_s": 600.0,
        "hatch_distance_mm": 0.12,
        "jump_speed_mm_s": 3000.0,
        "jump_delay_ms": None,
        "laser_power_w": None,
        "is_default": True,
        "notes": "Верифицировано по скриншотам LaserStudio 2026-06-17",
    },
    {
        "name": "12Х18Н10Т 32мкм (Нержавеющая сталь)",
        "material": "steel",
        "layer_thickness_mm": 0.032,
        "hatch_speed_mm_s": 1000.0,
        "contour_speed_mm_s": None,
        "hatch_distance_mm": 0.12,
        "jump_speed_mm_s": 3000.0,
        "jump_delay_ms": None,
        "laser_power_w": 400.0,
        "is_default": True,
        "notes": "Параметры приблизительные — точный пресет не сфотографирован",
    },
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table(_TABLE):
        Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[_TABLE]])

    # Use raw SQL for seed inserts — avoids sa.table()/sa.column() hang in
    # PostgreSQL transactional DDL context.
    existing = {
        row[0]
        for row in bind.execute(sa.text(f"SELECT material FROM {_TABLE}"))
    }

    for p in _SEED_PRESETS:
        if p["material"] in existing:
            continue
        bind.execute(
            sa.text(
                f"INSERT INTO {_TABLE} "
                "(name, material, layer_thickness_mm, hatch_speed_mm_s, "
                "contour_speed_mm_s, hatch_distance_mm, jump_speed_mm_s, "
                "jump_delay_ms, laser_power_w, is_default, notes, "
                "created_at, updated_at) "
                "VALUES (:name, :material, :layer_thickness_mm, :hatch_speed_mm_s, "
                ":contour_speed_mm_s, :hatch_distance_mm, :jump_speed_mm_s, "
                ":jump_delay_ms, :laser_power_w, :is_default, :notes, "
                "NOW(), NOW())"
            ),
            p,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        bind.execute(
            sa.text(f"DELETE FROM {_TABLE} WHERE material IN ('aluminum', 'steel')")
        )
