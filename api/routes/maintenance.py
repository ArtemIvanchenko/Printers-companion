"""Maintenance scheduler API."""

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import select

from domain.models.quality import MaintenanceRecord
from domain.services.maintenance import get_maintenance_status
from storage.db.session import session_scope

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/status")
def maintenance_status() -> list[dict]:
    with session_scope() as db:
        return get_maintenance_status(db)


@router.post("/reset/{component}")
def reset_component(component: str, notes: str = "") -> dict:
    """Record that a consumable was serviced / replaced (resets the wear clock)."""
    with session_scope() as db:
        rec = MaintenanceRecord(
            component=component,
            action="replaced_or_serviced",
            timestamp=datetime.now(timezone.utc),
            notes=notes or None,
        )
        db.add(rec)
        db.flush()
        return {"ok": True, "component": component, "timestamp": rec.timestamp.isoformat()}


@router.get("/history")
def maintenance_history() -> list[dict]:
    with session_scope() as db:
        recs = db.execute(
            select(MaintenanceRecord).order_by(MaintenanceRecord.timestamp.desc()).limit(100)
        ).scalars().all()
        return [
            {
                "id": r.maintenance_id,
                "component": r.component,
                "action": r.action,
                "timestamp": r.timestamp.isoformat(),
                "notes": r.notes,
            }
            for r in recs
        ]
