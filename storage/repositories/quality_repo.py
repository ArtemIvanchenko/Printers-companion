"""Quality repository for database operations."""
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.quality import QualityOutcome, Anomaly, MaintenanceRecord


class QualityRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def commit(self) -> None:
        self.db.commit()

    def save_outcome(self, outcome: QualityOutcome) -> None:
        existing = self.db.get(QualityOutcome, outcome.outcome_id)
        if existing:
            for key, value in jsonable_encoder(outcome).items():
                if key != "outcome_id":
                    setattr(existing, key, value)
        else:
            self.db.add(QualityOutcome(**jsonable_encoder(outcome)))

    def get_outcome(self, outcome_id: str) -> QualityOutcome | None:
        return self.db.get(QualityOutcome, outcome_id)

    def list_outcomes(self, session_id: str | None = None) -> list[QualityOutcome]:
        stmt = select(QualityOutcome)
        if session_id:
            stmt = stmt.where(QualityOutcome.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(QualityOutcome.created_at.desc())).all())

    def save_anomaly(self, anomaly: Anomaly) -> None:
        existing = self.db.get(Anomaly, anomaly.anomaly_id)
        if existing:
            for key, value in jsonable_encoder(anomaly).items():
                if key != "anomaly_id":
                    setattr(existing, key, value)
        else:
            self.db.add(Anomaly(**jsonable_encoder(anomaly)))

    def get_anomaly(self, anomaly_id: str) -> Anomaly | None:
        return self.db.get(Anomaly, anomaly_id)

    def list_anomalies(self, session_id: str | None = None) -> list[Anomaly]:
        stmt = select(Anomaly)
        if session_id:
            stmt = stmt.where(Anomaly.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(Anomaly.detected_at.desc())).all())

    def save_maintenance(self, record: MaintenanceRecord) -> None:
        existing = self.db.get(MaintenanceRecord, record.record_id)
        if existing:
            for key, value in jsonable_encoder(record).items():
                if key != "record_id":
                    setattr(existing, key, value)
        else:
            self.db.add(MaintenanceRecord(**jsonable_encoder(record)))

    def get_maintenance(self, record_id: str) -> MaintenanceRecord | None:
        return self.db.get(MaintenanceRecord, record_id)

    def list_maintenance(self, printer_id: str | None = None) -> list[MaintenanceRecord]:
        stmt = select(MaintenanceRecord)
        if printer_id:
            stmt = stmt.where(MaintenanceRecord.printer_id == printer_id)
        return list(self.db.scalars(stmt.order_by(MaintenanceRecord.performed_at.desc())).all())