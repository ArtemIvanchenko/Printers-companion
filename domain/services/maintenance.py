"""Compute wear for tracked consumables from historical BuildSession data.

Consumable thresholds come from M350 passport and TSF-400VAD manual:
  - Recoater blade:    replace at ~500 hours or 200 000 layers
  - HEPA filter:       replace every 200 h (TSF-400VAD section 15)
  - Protective glass:  replace every 500 h
  - Laser fiber:       service at 5 000 h (Yb fiber typical)
  - Inert gas filter:  replace every 300 h
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from domain.models.quality import MaintenanceRecord
from domain.models.sessions import BuildSession

# Consumable definitions: (display_name, unit, warning_pct, max_value)
_CONSUMABLES: dict[str, dict] = {
    "recoater_blade": {
        "label": "Ракель (нож)",
        "unit": "ч",
        "max": 500,
        "warn_pct": 80,
        "icon": "🔪",
        "note": "Паспорт M350: замена при износе; ~500 ч практика SLM",
    },
    "hepa_filter": {
        "label": "HEPA-фильтр",
        "unit": "ч",
        "max": 200,
        "warn_pct": 85,
        "icon": "🌬️",
        "note": "TSF-400VAD, раздел 15: периодическая замена",
    },
    "protective_glass": {
        "label": "Защитное стекло",
        "unit": "ч",
        "max": 500,
        "warn_pct": 80,
        "icon": "🔭",
        "note": "Рекомендуется раз в 500 ч печати",
    },
    "laser_fiber": {
        "label": "Лазерный источник",
        "unit": "ч",
        "max": 5000,
        "warn_pct": 90,
        "icon": "💡",
        "note": "Сервис иттербиевого волоконного лазера 500 Вт × 2",
    },
    "inert_gas_filter": {
        "label": "Фильтр инертного газа",
        "unit": "ч",
        "max": 300,
        "warn_pct": 80,
        "icon": "💨",
        "note": "Рециркуляция аргона/азота — M350 комплектность п.1.15",
    },
}


@dataclass
class ConsumableStatus:
    key: str
    label: str
    icon: str
    note: str
    unit: str
    used: float        # hours used since last service
    max_val: float     # max before replacement
    warn_pct: float    # warn threshold percent
    last_service: datetime | None
    grade: str         # "ok" | "warning" | "critical"
    pct: float         # 0–100 used
    remaining: float   # remaining hours


def _print_hours_since(sessions: list[BuildSession], since: datetime | None) -> float:
    """Sum REAL_PRINT session durations (in hours) after `since`.

    Classification is read from the context payload (authoritative) rather than
    the BuildSession.classification ORM column, which may lag on older imports.
    """
    total_sec = 0.0
    for s in sessions:
        ctx = (s.context or {}).get("runtime_payload", {}) or {}
        group = ctx.get("group", {}) or {}
        # Use payload classification; fall back to ORM column.
        cls = group.get("classification") or s.classification or ""
        if cls != "REAL_PRINT":
            continue
        if since and s.start_ts and s.start_ts < since:
            continue
        features = group.get("features", {}) or {}
        dur_min = features.get("duration_min") or 0.0
        total_sec += dur_min * 60
    return total_sec / 3600


def get_maintenance_status(db: DBSession) -> list[dict[str, Any]]:
    """Return wear status for all tracked consumables."""
    sessions = db.execute(
        select(BuildSession).order_by(BuildSession.start_ts)
    ).scalars().all()

    # Find last service datetime per component from maintenance records.
    records = db.execute(
        select(MaintenanceRecord).order_by(MaintenanceRecord.timestamp)
    ).scalars().all()

    last_service: dict[str, datetime] = {}
    for rec in records:
        key = rec.component
        if key in _CONSUMABLES:
            last_service[key] = rec.timestamp

    result = []
    for key, cfg in _CONSUMABLES.items():
        since = last_service.get(key)
        used_h = _print_hours_since(sessions, since)
        max_h = cfg["max"]
        pct = min(100.0, used_h / max_h * 100)
        remaining = max(0.0, max_h - used_h)

        if pct >= 100:
            grade = "critical"
        elif pct >= cfg["warn_pct"]:
            grade = "warning"
        else:
            grade = "ok"

        result.append({
            "key": key,
            "label": cfg["label"],
            "icon": cfg["icon"],
            "note": cfg["note"],
            "unit": cfg["unit"],
            "used": round(used_h, 1),
            "max": max_h,
            "warn_pct": cfg["warn_pct"],
            "pct": round(pct, 1),
            "remaining": round(remaining, 1),
            "last_service": since.isoformat() if since else None,
            "grade": grade,
        })

    return result
