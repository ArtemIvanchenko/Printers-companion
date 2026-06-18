"""Repository for print records, attached files and machine parameters."""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete as sql_delete, func, select
from sqlalchemy.orm import Session

from domain.models.prints import MachineParams, PrintRecord, PrintRecordFile

_RECORD_FIELDS = (
    "name", "material", "session_id", "status", "notes", "metadata_json",
    "printed_at", "powder_cost_rub_per_kg",
)
_PARAM_FIELDS = (
    "hatch_speed_mm_s", "contour_speed_mm_s", "hatch_distance_mm", "layer_thickness_mm",
    "laser_count", "recoat_time_ms", "time_correction_factor",
    "jump_speed_mm_s", "jump_delay_ms",
    "powder_cost_rub_per_kg", "gas_cost_rub_per_atm", "gas_atm_per_print",
    "filter_cost_rub", "filter_lifetime_hours", "platform_cost_rub",
    "material_densities", "hatch_speeds_by_mat", "build_area_cm2",
)


def _record_to_dict(row: PrintRecord) -> dict[str, Any]:
    return {
        "record_id": row.record_id,
        "name": row.name,
        "material": row.material,
        "session_id": row.session_id,
        "status": row.status,
        "notes": row.notes,
        "metadata_json": row.metadata_json or {},
        "printed_at": row.printed_at.isoformat() if row.printed_at else None,
        "powder_cost_rub_per_kg": row.powder_cost_rub_per_kg,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _file_to_dict(row: PrintRecordFile) -> dict[str, Any]:
    return {
        "file_id": row.file_id,
        "record_id": row.record_id,
        "object_uri": row.object_uri,
        "file_name": row.file_name,
        "file_type": row.file_type,
        "size_bytes": row.size_bytes,
        "checksum": row.checksum,
        "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
    }


def _params_to_dict(row: MachineParams) -> dict[str, Any]:
    data: dict[str, Any] = {field: getattr(row, field) for field in _PARAM_FIELDS}
    data["updated_at"] = row.updated_at.isoformat() if row.updated_at else None
    return data


class PrintsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def flush(self) -> None:
        """Flush pending changes within the unit of work; the boundary commits."""
        self.db.flush()

    # ── Print records ──────────────────────────────────────────────────────

    def create_print_record(self, values: dict[str, Any]) -> dict[str, Any]:
        row = PrintRecord(**{k: v for k, v in values.items() if k in _RECORD_FIELDS})
        self.db.add(row)
        self.db.flush()
        return _record_to_dict(row)

    def update_print_record(self, record_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        row = self.db.get(PrintRecord, record_id)
        if not row:
            return None
        for key in _RECORD_FIELDS:
            if key in values:
                setattr(row, key, values[key])
        row.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return _record_to_dict(row)

    def get_print_record(self, record_id: str) -> dict[str, Any] | None:
        row = self.db.get(PrintRecord, record_id)
        return _record_to_dict(row) if row else None

    def _filtered_records(
        self,
        query: str | None = None,
        material: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ):
        # printed_at is the real print date; fall back to created_at when unknown
        effective_date = func.coalesce(PrintRecord.printed_at, PrintRecord.created_at)
        stmt = select(PrintRecord)
        if query:
            stmt = stmt.where(PrintRecord.name.ilike(f"%{query}%"))
        if material:
            stmt = stmt.where(PrintRecord.material == material)
        if date_from:
            stmt = stmt.where(effective_date >= date_from)
        if date_to:
            stmt = stmt.where(effective_date <= date_to)
        return stmt, effective_date

    def list_print_records(
        self,
        skip: int = 0,
        limit: int = 50,
        query: str | None = None,
        material: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[dict[str, Any]]:
        stmt, effective_date = self._filtered_records(query, material, date_from, date_to)
        rows = self.db.scalars(
            stmt.order_by(effective_date.desc()).offset(skip).limit(limit)
        ).all()
        return [_record_to_dict(row) for row in rows]

    def count_print_records(
        self,
        query: str | None = None,
        material: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> int:
        stmt, _ = self._filtered_records(query, material, date_from, date_to)
        return int(self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)

    def last_powder_cost(self) -> float | None:
        """Most recently used powder price: latest record snapshot, else machine params."""
        value = self.db.scalar(
            select(PrintRecord.powder_cost_rub_per_kg)
            .where(PrintRecord.powder_cost_rub_per_kg.is_not(None))
            .order_by(PrintRecord.created_at.desc())
            .limit(1)
        )
        if value is not None:
            return float(value)
        params = self.db.get(MachineParams, 1)
        return params.powder_cost_rub_per_kg if params else None

    def delete_print_record(self, record_id: str) -> list[str]:
        """Delete a record with its file rows; returns object URIs for storage cleanup."""
        row = self.db.get(PrintRecord, record_id)
        if not row:
            return []
        uris = [
            f.object_uri
            for f in self.db.scalars(
                select(PrintRecordFile).where(PrintRecordFile.record_id == record_id)
            ).all()
        ]
        # Delete child rows first via SQL to avoid FK ordering issues with ORM unit-of-work
        self.db.execute(sql_delete(PrintRecordFile).where(PrintRecordFile.record_id == record_id))
        self.db.delete(row)
        self.db.flush()
        return uris

    def delete_print_file(self, record_id: str, file_id: str) -> str | None:
        """Delete one attached file row; returns its object URI or None."""
        row = self.db.get(PrintRecordFile, file_id)
        if not row or row.record_id != record_id:
            return None
        uri = row.object_uri
        self.db.delete(row)
        self.db.flush()
        return uri

    def find_unlinked_records_near(self, ts: datetime, window_hours: float = 24.0) -> list[dict[str, Any]]:
        """Records without a session whose print date falls within ±window of ts."""
        from datetime import timedelta

        lo, hi = ts - timedelta(hours=window_hours), ts + timedelta(hours=window_hours)
        effective_date = func.coalesce(PrintRecord.printed_at, PrintRecord.created_at)
        rows = self.db.scalars(
            select(PrintRecord)
            .where(PrintRecord.session_id.is_(None))
            .where(effective_date >= lo)
            .where(effective_date <= hi)
        ).all()
        return [_record_to_dict(row) for row in rows]

    def link_session(self, record_id: str, session_id: str, session_start: datetime | None = None) -> bool:
        """Attach a log session; its start timestamp becomes the authoritative print date."""
        row = self.db.get(PrintRecord, record_id)
        if not row:
            return False
        row.session_id = session_id
        if session_start is not None:
            row.printed_at = session_start
        row.updated_at = datetime.now(timezone.utc)
        return True

    # ── Attached files ─────────────────────────────────────────────────────

    def add_print_file(self, values: dict[str, Any]) -> dict[str, Any]:
        row = PrintRecordFile(
            record_id=values["record_id"],
            object_uri=values["object_uri"],
            file_name=values["file_name"],
            file_type=values["file_type"],
            size_bytes=int(values.get("size_bytes") or 0),
            checksum=values.get("checksum") or "",
        )
        self.db.add(row)
        self.db.flush()
        return _file_to_dict(row)

    def list_print_files(self, record_id: str) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(PrintRecordFile)
            .where(PrintRecordFile.record_id == record_id)
            .order_by(PrintRecordFile.uploaded_at.asc())
        ).all()
        return [_file_to_dict(row) for row in rows]

    def find_file_by_checksum(self, record_id: str, checksum: str) -> dict[str, Any] | None:
        row = self.db.scalars(
            select(PrintRecordFile)
            .where(PrintRecordFile.record_id == record_id)
            .where(PrintRecordFile.checksum == checksum)
        ).first()
        return _file_to_dict(row) if row else None

    # ── Machine parameters (single row, id=1) ──────────────────────────────

    def get_machine_params(self) -> dict[str, Any] | None:
        row = self.db.get(MachineParams, 1)
        return _params_to_dict(row) if row else None

    def save_machine_params(self, values: dict[str, Any]) -> dict[str, Any]:
        row = self.db.get(MachineParams, 1)
        if not row:
            row = MachineParams(id=1)
            self.db.add(row)
        for key in _PARAM_FIELDS:
            if key in values:
                setattr(row, key, values[key])
        row.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        return _params_to_dict(row)
