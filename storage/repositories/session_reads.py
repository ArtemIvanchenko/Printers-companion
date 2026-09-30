"""SQL-only, detached read models for sessions and published reports.

No filesystem, parser or object-store calls belong in this repository. The
application service closes its read transaction before expanding an artifact.
"""

from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models.sessions import BuildSession, ReportArtifact


class SessionReadsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _visible_payload():
        payload = BuildSession.context["runtime_payload"].as_string()
        return payload.is_not(None) & payload.not_in(("null", "{}"))

    def list_payloads(self, *, skip: int = 0, limit: int = 100) -> list[tuple[str, dict]]:
        rows = self.db.execute(
            select(BuildSession.session_id, BuildSession.context["runtime_payload"])
            .where(self._visible_payload())
            .order_by(BuildSession.created_at.desc(), BuildSession.session_id.desc())
            .offset(skip).limit(limit)
        ).all()
        return [(sid, deepcopy(payload)) for sid, payload in rows]

    def count_payloads(self) -> int:
        return int(self.db.scalar(
            select(func.count()).select_from(BuildSession).where(self._visible_payload())
        ) or 0)

    def session_snapshot(self, session_id: str) -> dict | None:
        row = self.db.execute(select(
            BuildSession.origin_compute_node_id, BuildSession.context,
        ).where(BuildSession.session_id == session_id)).one_or_none()
        if row is None:
            return None
        return {"owner_node_id": row[0], "context": deepcopy(row[1] or {})}

    def latest_report(self, session_id: str) -> dict | None:
        row = self.db.execute(select(
            ReportArtifact.report_id, ReportArtifact.storage_uri, ReportArtifact.payload,
        ).where(ReportArtifact.session_id == session_id)
            .order_by(ReportArtifact.generated_at.desc(), ReportArtifact.report_id.desc())
            .limit(1)).one_or_none()
        if row is None:
            return None
        return {"report_id": row[0], "storage_uri": row[1], "payload": deepcopy(row[2] or {})}

    def report_index(self, session_id: str) -> list[dict]:
        rows = self.db.execute(select(
            ReportArtifact.report_id, ReportArtifact.session_id, ReportArtifact.generated_at,
        ).where(ReportArtifact.session_id == session_id)
            .order_by(ReportArtifact.generated_at.desc(), ReportArtifact.report_id.desc())).all()
        return [{"report_id": rid, "session_id": sid,
                 "generated_at": timestamp.isoformat() if timestamp else None}
                for rid, sid, timestamp in rows]
