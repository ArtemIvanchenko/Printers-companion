"""Authoritative lease checks inside the transaction that writes import data."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.sessions import ImportJob


class StaleImportLeaseError(RuntimeError):
    """This worker no longer has permission to publish the import."""


@dataclass(frozen=True)
class ImportFence:
    import_job_id: str
    owner_node_id: str
    lease_owner: str
    lease_generation: int

    def verify(self, db: Session) -> ImportJob:
        """Lock, then check fresh time/state; never renew via another connection."""
        row = db.scalar(
            select(ImportJob)
            .where(ImportJob.import_job_id == self.import_job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        until = row.lease_until if row else None
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if (
            row is None
            or row.owner_node_id != self.owner_node_id
            or row.lease_owner != self.lease_owner
            or row.lease_generation != self.lease_generation
            or row.status not in {"checking_stability", "postponed"}
            or until is None
            or until <= datetime.now(timezone.utc)
        ):
            raise StaleImportLeaseError(
                "Право на импорт истекло или было заменено; результат не сохранён."
            )
        return row

    def as_dict(self) -> dict:
        return {
            "import_job_id": self.import_job_id,
            "owner_node_id": self.owner_node_id,
            "lease_generation": self.lease_generation,
        }
