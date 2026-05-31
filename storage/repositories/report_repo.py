"""Report repository for database operations."""
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.sessions import ReportArtifact


class ReportRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def commit(self) -> None:
        self.db.commit()

    def save_artifact(self, report_id: str, report_type: str, session_id: str, artifact: dict) -> None:
        existing = self.db.get(ReportArtifact, report_id)
        if existing:
            existing.artifact = jsonable_encoder(artifact)
            existing.updated_at = datetime.now(timezone.utc)
        else:
            self.db.add(
                ReportArtifact(
                    report_id=report_id,
                    report_type=report_type,
                    session_id=session_id,
                    artifact=jsonable_encoder(artifact),
                )
            )

    def get_artifact(self, report_id: str) -> dict | None:
        row = self.db.get(ReportArtifact, report_id)
        if not row:
            return None
        return row.artifact

    def list_artifacts(self, session_id: str | None = None) -> list[ReportArtifact]:
        stmt = select(ReportArtifact)
        if session_id:
            stmt = stmt.where(ReportArtifact.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(ReportArtifact.created_at.desc())).all())