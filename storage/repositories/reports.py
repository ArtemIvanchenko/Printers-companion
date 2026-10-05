"""SQL-only persistence of published report artifacts; caller owns the transaction."""

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.sessions import ReportArtifact


class ReportsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _snapshot(self, statement) -> dict | None:
        row = self.db.scalar(statement.execution_options(populate_existing=True))
        if row is None:
            return None
        return deepcopy({
            "report_id": row.report_id, "session_id": row.session_id,
            "report_type": row.report_type, "storage_uri": row.storage_uri,
            "payload": row.payload or {}, "version_metadata": row.version_metadata or {},
        })

    def get(self, report_id: str, *, for_update: bool = False) -> dict | None:
        statement = select(ReportArtifact).where(ReportArtifact.report_id == report_id)
        return self._snapshot(statement.with_for_update() if for_update else statement)

    def latest(self, session_id: str) -> dict | None:
        return self._snapshot(select(ReportArtifact)
            .where(ReportArtifact.session_id == session_id)
            .order_by(ReportArtifact.generated_at.desc(), ReportArtifact.report_id.desc())
            .limit(1))

    def index(self, session_id: str) -> list[dict]:
        rows = self.db.execute(select(
            ReportArtifact.report_id, ReportArtifact.session_id, ReportArtifact.generated_at,
        ).where(ReportArtifact.session_id == session_id)
            .order_by(ReportArtifact.generated_at.desc(), ReportArtifact.report_id.desc())).all()
        return [{"report_id": rid, "session_id": sid,
                 "generated_at": timestamp.isoformat() if timestamp else None}
                for rid, sid, timestamp in rows]

    def save_prepared(self, report_id: str, prepared: dict, *, report_type: str = "session") -> None:
        """Publish already uploaded bytes, without object-store IO or commit."""
        row = self.db.get(ReportArtifact, report_id)
        if row is None:
            row = ReportArtifact(report_id=report_id)
            self.db.add(row)
        row.session_id = prepared["payload"].get("session_id")
        row.report_type = report_type
        row.storage_uri = prepared["storage_uri"]
        row.payload = prepared["payload"]
        row.version_metadata = prepared["version_metadata"]
        self.db.flush()
