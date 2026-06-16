import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from uuid import uuid4

from storage.repositories.runtime import RuntimeRepository


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
        from domain.models.entities import BuildSession

        mock_db_session.get.return_value = None

        repo = RuntimeRepository(mock_db_session)
        repo.save_session_payload("session_123", {"files": [], "group": {"features": {}}})

        mock_db_session.add.assert_called_once()
        mock_db_session.flush.assert_called_once()

    def test_save_session_payload_existing(self, mock_db_session):
        """Test updating existing session payload."""
        from domain.models.entities import BuildSession

        existing = MagicMock()
        existing.context = {}
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
        from domain.models.entities import BuildSession

        session = MagicMock()
        session.context = {"runtime_payload": {"files": [], "group": {}}}
        mock_db_session.get.return_value = session

        repo = RuntimeRepository(mock_db_session)
        result = repo.get_session_payload("session_123")

        assert result is not None
        assert "files" in result


class TestRuntimeRepositoryReports:
    """Test report-related methods."""

    def test_save_report_new(self, mock_db_session):
        """Test saving new report."""
        from domain.models.entities import ReportArtifact

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
        from domain.models.entities import OperatorEvent

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