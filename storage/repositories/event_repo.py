"""Event repository for database operations."""
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.events import OperatorEvent, OperatorJournalEntry, CanonicalEvent


class EventRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def commit(self) -> None:
        self.db.commit()

    def save_event(self, event: OperatorEvent) -> None:
        existing = self.db.get(OperatorEvent, event.event_id)
        if existing:
            for key, value in jsonable_encoder(event).items():
                if key != "event_id":
                    setattr(existing, key, value)
        else:
            self.db.add(OperatorEvent(**jsonable_encoder(event)))

    def get_event(self, event_id: str) -> OperatorEvent | None:
        return self.db.get(OperatorEvent, event_id)

    def list_events(self, session_id: str | None = None, limit: int = 100) -> list[OperatorEvent]:
        stmt = select(OperatorEvent).order_by(OperatorEvent.occurred_at.desc())
        if session_id:
            stmt = stmt.where(OperatorEvent.session_id == session_id)
        return list(self.db.scalars(stmt.limit(limit)).all())

    def save_journal_entry(self, entry: OperatorJournalEntry) -> None:
        existing = self.db.get(OperatorJournalEntry, entry.journal_id)
        if existing:
            for key, value in jsonable_encoder(entry).items():
                if key != "journal_id":
                    setattr(existing, key, value)
        else:
            self.db.add(OperatorJournalEntry(**jsonable_encoder(entry)))

    def get_journal_entry(self, journal_id: str) -> OperatorJournalEntry | None:
        return self.db.get(OperatorJournalEntry, journal_id)

    def list_journal_entries(self, session_id: str) -> list[OperatorJournalEntry]:
        stmt = select(OperatorJournalEntry).where(OperatorJournalEntry.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(OperatorJournalEntry.occurred_at.desc())).all())

    def save_canonical_event(self, event: CanonicalEvent) -> None:
        existing = self.db.get(CanonicalEvent, event.event_id)
        if existing:
            for key, value in jsonable_encoder(event).items():
                if key != "event_id":
                    setattr(existing, key, value)
        else:
            self.db.add(CanonicalEvent(**jsonable_encoder(event)))

    def get_canonical_event(self, event_id: str) -> CanonicalEvent | None:
        return self.db.get(CanonicalEvent, event_id)

    def list_canonical_events(self, session_id: str | None = None) -> list[CanonicalEvent]:
        stmt = select(CanonicalEvent)
        if session_id:
            stmt = stmt.where(CanonicalEvent.session_id == session_id)
        return list(self.db.scalars(stmt.order_by(CanonicalEvent.occurred_at.asc())).all())