"""Powder lifecycle tracking API."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from domain.models.quality import MaterialBatch, PowderPreparationEvent, PowderUsageCycle
from storage.db.session import session_scope

router = APIRouter(prefix="/powder", tags=["powder"])

# M350 passport: build volume 350×350×390 mm, layer 16–92 µm.
# Typical AlSi10Mg bulk density ~1.45 g/cm³ at 50% packing.
_DEFAULT_KG_PER_LAYER = 0.050   # ~50 g per layer at 350×350 mm footprint (estimate)


class BatchCreate(BaseModel):
    material: str
    alloy: str | None = None
    batch_code: str
    supplier: str | None = None
    initial_mass_kg: float
    notes: str | None = None


class ConsumeRequest(BaseModel):
    session_id: str | None = None
    mass_kg: float | None = None
    layers: int | None = None
    notes: str | None = None


@router.post("/batches")
def create_batch(body: BatchCreate) -> dict:
    with session_scope() as db:
        batch = MaterialBatch(
            material=body.material,
            alloy=body.alloy,
            batch_code=body.batch_code,
            supplier=body.supplier,
            payload={"initial_mass_kg": body.initial_mass_kg, "notes": body.notes},
        )
        db.add(batch)
        cycle = PowderUsageCycle(
            material_batch_id=batch.material_batch_id,
            powder_batch=body.batch_code,
            reuse_count=0,
            started_at=datetime.now(timezone.utc),
            history=[{"event": "loaded", "kg": body.initial_mass_kg,
                       "ts": datetime.now(timezone.utc).isoformat()}],
        )
        db.add(cycle)
        db.flush()
        return {"batch_id": batch.material_batch_id, "cycle_id": cycle.powder_cycle_id}


@router.get("/batches")
def list_batches() -> list[dict]:
    with session_scope() as db:
        batches = db.execute(
            select(MaterialBatch).order_by(MaterialBatch.created_at.desc()).limit(50)
        ).scalars().all()
        result = []
        for b in batches:
            cycles = db.execute(
                select(PowderUsageCycle)
                .where(PowderUsageCycle.material_batch_id == b.material_batch_id)
                .order_by(PowderUsageCycle.started_at.desc())
            ).scalars().all()
            latest_cycle = cycles[0] if cycles else None
            consumed = sum(
                ev.get("kg", 0) for c in cycles
                for ev in (c.history or []) if ev.get("event") == "consumed"
            )
            result.append({
                "batch_id": b.material_batch_id,
                "material": b.material,
                "alloy": b.alloy,
                "batch_code": b.batch_code,
                "supplier": b.supplier,
                "initial_kg": b.payload.get("initial_mass_kg", 0),
                "consumed_kg": round(consumed, 3),
                "remaining_kg": round(b.payload.get("initial_mass_kg", 0) - consumed, 3),
                "reuse_count": latest_cycle.reuse_count if latest_cycle else 0,
                "loaded_at": b.created_at.isoformat(),
                "active_cycle": latest_cycle.powder_cycle_id if latest_cycle else None,
            })
        return result


@router.post("/batches/{batch_id}/consume")
def log_consumption(batch_id: str, body: ConsumeRequest) -> dict:
    with session_scope() as db:
        batch = db.get(MaterialBatch, batch_id)
        if not batch:
            raise HTTPException(404, "Batch not found")

        cycle = db.execute(
            select(PowderUsageCycle)
            .where(PowderUsageCycle.material_batch_id == batch_id)
            .order_by(PowderUsageCycle.started_at.desc())
        ).scalars().first()
        if not cycle:
            raise HTTPException(404, "No active cycle for this batch")

        kg = body.mass_kg
        if kg is None and body.layers:
            kg = body.layers * _DEFAULT_KG_PER_LAYER

        history = list(cycle.history or [])
        history.append({
            "event": "consumed",
            "kg": round(kg or 0, 4),
            "layers": body.layers,
            "session_id": body.session_id,
            "notes": body.notes,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        cycle.history = history
        cycle.reuse_count = sum(1 for h in history if h.get("event") == "sieved")
        db.add(
            PowderPreparationEvent(
                powder_cycle_id=cycle.powder_cycle_id,
                timestamp=datetime.now(timezone.utc),
                event_type="consumed",
                value=str(round(kg or 0, 4)),
                unit="kg",
                payload={"session_id": body.session_id, "layers": body.layers},
            )
        )
        db.flush()
        return {"ok": True, "consumed_kg": kg, "cycle_id": cycle.powder_cycle_id}


@router.get("/status")
def powder_status() -> dict[str, Any]:
    """Summary of the active (most recent) powder batch."""
    with session_scope() as db:
        batches = db.execute(
            select(MaterialBatch).order_by(MaterialBatch.created_at.desc()).limit(1)
        ).scalars().first()
        if not batches:
            return {"has_batch": False}

        cycles = db.execute(
            select(PowderUsageCycle)
            .where(PowderUsageCycle.material_batch_id == batches.material_batch_id)
            .order_by(PowderUsageCycle.started_at.desc())
        ).scalars().all()

        consumed = sum(
            ev.get("kg", 0) for c in cycles
            for ev in (c.history or []) if ev.get("event") == "consumed"
        )
        initial = batches.payload.get("initial_mass_kg", 0)
        reuse = max((c.reuse_count for c in cycles), default=0)
        # Quality degrades above ~10 reuse cycles for AlSi alloys.
        quality_pct = max(0, 100 - reuse * 8)

        return {
            "has_batch": True,
            "batch_id": batches.material_batch_id,
            "material": batches.material,
            "alloy": batches.alloy,
            "batch_code": batches.batch_code,
            "initial_kg": initial,
            "consumed_kg": round(consumed, 3),
            "remaining_kg": round(max(0, initial - consumed), 3),
            "remaining_pct": round(max(0, (initial - consumed) / initial * 100), 1) if initial else 0,
            "reuse_count": reuse,
            "quality_pct": quality_pct,
            "quality_grade": "ok" if quality_pct >= 70 else "warning" if quality_pct >= 40 else "critical",
        }
