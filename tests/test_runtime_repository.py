from unittest.mock import MagicMock
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from core.config.settings import get_settings
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


class TestRuntimeRepositoryUpsert:
    """Test _upsert helper method."""

    def test_upsert_creates_new_entity(self, mock_db_session):
        """Test _upsert creates new entity when not exists."""
        from domain.models.entities import OperatorEvent

        repo = RuntimeRepository(mock_db_session)
        mock_db_session.get.return_value = None

        result = repo._upsert(
            OperatorEvent,
            "test_event_123",
            "event_id",
            {"event_type": "test", "value": "100"}
        )

        mock_db_session.add.assert_called_once()
        assert result is not None

    def test_upsert_updates_existing_entity(self, mock_db_session):
        """Test _upsert updates existing entity."""
        from domain.models.entities import OperatorEvent

        existing = MagicMock()
        mock_db_session.get.return_value = existing

        repo = RuntimeRepository(mock_db_session)
        result = repo._upsert(
            OperatorEvent,
            "test_event_123",
            "event_id",
            {"event_type": "updated"}
        )

        existing.event_type = "updated"
        mock_db_session.add.assert_not_called()
        assert result == existing


class TestRuntimeRepositorySessions:
    """Test session-related methods."""

    def test_save_session_payload_new(self, mock_db_session):
        """Test saving new session payload."""

        mock_db_session.get.return_value = None

        repo = RuntimeRepository(mock_db_session)
        repo.save_session_payload("session_123", {"files": [], "group": {"features": {}}})

        mock_db_session.add.assert_called_once()
        mock_db_session.flush.assert_called_once()

    def test_save_session_payload_existing(self, mock_db_session):
        """Test updating existing session payload."""

        existing = MagicMock()
        existing.context = {}
        existing.origin_compute_node_id = get_settings().compute_node_id
        mock_db_session.get.return_value = existing

        repo = RuntimeRepository(mock_db_session)
        repo.save_session_payload("session_123", {"files": [], "group": {"features": {}}})

        mock_db_session.add.assert_not_called()
        mock_db_session.flush.assert_called_once()

    def test_get_session_payload_not_found(self, mock_db_session):
        """Test getting non-existent session."""
        mock_db_session.get.return_value = None

        repo = RuntimeRepository(mock_db_session)
        result = repo.get_session_payload("session_123")

        assert result is None

    def test_get_session_payload_found(self, mock_db_session):
        """Test getting existing session."""

        session = MagicMock()
        session.context = {"runtime_payload": {"files": [], "group": {}}}
        mock_db_session.get.return_value = session

        repo = RuntimeRepository(mock_db_session)
        result = repo.get_session_payload("session_123")

        assert result is not None
        assert "files" in result


class TestSessionClassificationColumn:
    """The classification column must track the payload, not keep its default.

    Real DB, not a mock: the bug this covers was that the column was simply
    never assigned, which a MagicMock happily accepts (every attribute write
    "succeeds"). Only a real row shows the default surviving.
    """

    @staticmethod
    def _payload(classification: str, confidence: float = 0.78) -> dict:
        return {"files": [], "group": {
            "classification": classification, "confidence": confidence, "features": {},
        }}

    def test_classification_written_on_create(self, db):
        from domain.models.sessions import BuildSession

        RuntimeRepository(db).save_session_payload("s_new", self._payload("REAL_PRINT"))

        row = db.get(BuildSession, "s_new")
        assert row.classification == "REAL_PRINT"
        assert row.classification_confidence == pytest.approx(0.78)

    def test_classification_updated_on_reimport(self, db):
        from domain.models.sessions import BuildSession

        repo = RuntimeRepository(db)
        repo.save_session_payload("s_up", self._payload("INCOMPLETE_OR_UNKNOWN", 0.2))
        # A re-import with the full file set reclassifies the same session.
        repo.save_session_payload("s_up", self._payload("REAL_PRINT", 0.9))

        row = db.get(BuildSession, "s_up")
        assert row.classification == "REAL_PRINT"
        assert row.classification_confidence == pytest.approx(0.9)

    def test_classification_is_sql_filterable(self, db):
        """The point of the column: expressing "only real prints" in SQL."""
        from sqlalchemy import select

        from domain.models.sessions import BuildSession

        repo = RuntimeRepository(db)
        repo.save_session_payload("s_real", self._payload("REAL_PRINT"))
        repo.save_session_payload("s_pre", self._payload("PRE_BURN_SESSION"))

        found = db.scalars(
            select(BuildSession.session_id).where(BuildSession.classification == "REAL_PRINT")
        ).all()
        assert found == ["s_real"]

    def test_payload_without_classification_keeps_previous(self, db):
        """A partial payload must not wipe a known classification back to default."""
        from domain.models.sessions import BuildSession

        repo = RuntimeRepository(db)
        repo.save_session_payload("s_keep", self._payload("REAL_PRINT"))
        repo.save_session_payload("s_keep", {"files": [], "group": {"features": {}}})

        assert db.get(BuildSession, "s_keep").classification == "REAL_PRINT"


class TestRuntimeRepositoryReports:
    """Test report-related methods."""

    def test_save_report_new(self, mock_db_session):
        """Test saving new report."""

        mock_db_session.get.return_value = None
        report = {
            "report_id": "report_123",
            "session_id": "session_123",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        repo = RuntimeRepository(mock_db_session)
        repo.save_report(report)

        mock_db_session.add.assert_called_once()
        mock_db_session.flush.assert_called_once()


class TestRuntimeRepositoryOperatorEvents:
    """Test operator event methods."""

    def test_save_operator_event_new(self, mock_db_session, sample_operator_event):
        """Test saving new operator event."""
        mock_db_session.get.return_value = None

        repo = RuntimeRepository(mock_db_session)
        repo.save_operator_event(sample_operator_event)

        mock_db_session.add.assert_called_once()
        mock_db_session.flush.assert_called_once()

    def test_save_operator_event_existing(self, mock_db_session, sample_operator_event):
        """Test updating existing operator event."""

        existing = MagicMock()
        mock_db_session.get.return_value = existing

        repo = RuntimeRepository(mock_db_session)
        repo.save_operator_event(sample_operator_event)

        mock_db_session.add.assert_not_called()

    def test_list_operator_events_empty(self, mock_db_session):
        """Test listing empty operator events."""
        mock_db_session.scalars.return_value.all.return_value = []

        repo = RuntimeRepository(mock_db_session)
        result = repo.list_operator_events()

        assert result == []


class TestRuntimeRepositoryQuality:
    """Test quality outcome methods."""

    def test_save_quality_outcome(self, mock_db_session):
        """Test saving quality outcome."""
        mock_db_session.get.return_value = None
        outcome = {
            "outcome_id": f"quality_{uuid4().hex[:8]}",
            "session_id": "session_123",
            "result": "accepted",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        repo = RuntimeRepository(mock_db_session)
        repo.save_quality_outcome(outcome)

        mock_db_session.add.assert_called_once()
        mock_db_session.flush.assert_called_once()
