"""SQL-only, detached read models for session context.

No filesystem, parser or object-store calls belong in this repository. The
application service closes its read transaction before expanding an artifact.
"""

from copy import deepcopy

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.models.sessions import BuildSession


class SessionReadsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _visible_payload():
        payload = BuildSession.context["runtime_payload"].as_string()
        return payload.is_not(None) & payload.not_in(("null", "{}"))

    def list_groups(self, *, skip: int = 0, limit: int = 100) -> list[tuple[str, dict]]:
        rows = self.db.execute(
            select(BuildSession.session_id, BuildSession.context["runtime_payload"]["group"])
            .where(self._visible_payload())
            .order_by(BuildSession.created_at.desc(), BuildSession.session_id.desc())
            .offset(skip).limit(limit)
        ).all()
        return [(sid, deepcopy(group or {})) for sid, group in rows]

    def count_payloads(self) -> int:
        return int(self.db.scalar(
            select(func.count()).select_from(BuildSession).where(self._visible_payload())
        ) or 0)

    def latest_id(self, *, compute_node_id: str) -> str | None:
        """Latest owner-local session by start, without loading JSON payloads."""
        return self.db.scalar(select(BuildSession.session_id).where(
            self._visible_payload(), BuildSession.origin_compute_node_id == compute_node_id,
        )
            .order_by(func.coalesce(BuildSession.context["runtime_payload"]["group"]["start_ts"].as_string(), "").desc(),
                      BuildSession.created_at.desc(), BuildSession.session_id.desc()).limit(1))

    def session_snapshot(self, session_id: str, *, for_update: bool = False) -> dict | None:
        statement = select(
            BuildSession.origin_compute_node_id, BuildSession.context,
        ).where(BuildSession.session_id == session_id)
        row = self.db.execute(statement.with_for_update() if for_update else statement).one_or_none()
        if row is None:
            return None
        return {"owner_node_id": row[0], "context": deepcopy(row[1] or {})}

    def sources_snapshot(self, session_id: str) -> dict | None:
        """Copy only source metadata and owner, not the large display projection."""
        row = self.db.execute(select(
            BuildSession.origin_compute_node_id,
            BuildSession.context["runtime_payload"]["files"],
        ).where(BuildSession.session_id == session_id, self._visible_payload())).one_or_none()
        if row is None:
            return None
        return {"owner_node_id": row[0], "files": deepcopy(row[1] or [])}
