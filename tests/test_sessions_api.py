import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone
from uuid import uuid4


class TestSessionsAPI:
    """Test API sessions routes."""

    @pytest.fixture
    def mock_repo(self):
        """Mock RuntimeRepository."""
        repo = MagicMock()
        repo.get_session_payload.return_value = None
        repo.get_session_files.return_value = None
        repo.list_session_payloads.return_value = []
        repo.save_report = MagicMock()
        repo.commit = MagicMock()
        return repo

    def test_list_sessions_empty(self, mock_repo):
        """Test listing sessions when none exist (paginated contract)."""
        from api.routes.sessions import list_sessions

        mock_repo.list_session_payloads.return_value = []
        result = list_sessions(repo=mock_repo)

        assert result["items"] == []
        assert result["total"] == 0

    def test_list_sessions_with_data(self, mock_repo):
        """Test listing sessions with data (paginated contract)."""
        from api.routes.sessions import list_sessions

        mock_repo.list_session_payloads.return_value = [
            ("session_1", {"group": {"features": {}}}),
            ("session_2", {"group": {"features": {}}}),
        ]

        result = list_sessions(repo=mock_repo)

        assert result["total"] == 2
        assert len(result["items"]) == 2
        assert result["items"][0]["session_id"] == "session_1"

    def test_get_session_not_found(self, mock_repo):
        """Test getting non-existent session."""
        from fastapi import HTTPException
        from api.routes.sessions import get_session

        mock_repo.get_session_payload.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            get_session("session_123", repo=mock_repo)

        assert exc_info.value.status_code == 404

    def test_get_session_found(self, mock_repo):
        """Test getting existing session."""
        from api.routes.sessions import get_session

        mock_repo.get_session_payload.return_value = {
            "group": {
                "features": {"material": "AlSi10Mg"},
                "confidence": 0.95,
            }
        }

        result = get_session("session_123", repo=mock_repo)

        assert result["session_id"] == "session_123"
        assert result["features"]["material"] == "AlSi10Mg"


class TestSessionsReportGeneration:
    """Test report generation in sessions API."""

    @pytest.fixture
    def mock_repo(self):
        """Mock RuntimeRepository."""
        repo = MagicMock()
        repo.get_session_files.return_value = []
        repo.save_report = MagicMock()
        repo.commit = MagicMock()
        return repo

    @patch("api.routes.sessions.generate_session_json_report")
    def test_generate_report_new(self, mock_generate, mock_repo):
        """Test generating new report."""
        from api.routes.sessions import _generate_report

        mock_generate.return_value = {
            "report_id": "report_123",
            "session_id": "session_123",
            "timeline": [],
            "phase_segments": [],
            "file_inventory": [],
            "data_quality": {"parse_diagnostics": []},
        }

        result = _generate_report("session_123", include_markdown=False, repo=mock_repo)

        assert result["report_id"] == "report_123"
        mock_repo.save_report.assert_called_once()
        mock_repo.flush.assert_called_once()

    @patch("api.routes.sessions.generate_session_json_report")
    def test_generate_report_with_markdown(self, mock_generate, mock_repo):
        """Test generating report with markdown."""
        from api.routes.sessions import _generate_report
        from unittest.mock import MagicMock as MockReport

        mock_generate.return_value = {
            "report_id": "report_123",
            "session_id": "session_123",
            "timeline": [],
            "phase_segments": [],
            "file_inventory": [],
            "data_quality": {"parse_diagnostics": []},
        }

        with patch("api.routes.sessions.generate_markdown_report") as mock_md:
            mock_md.return_value = "# Report"

            result = _generate_report("session_123", include_markdown=True, repo=mock_repo)

            assert "markdown" in result

    def test_report_cache(self):
        """Test report caching mechanism."""
        from api.routes.sessions import _report_cache, _invalidate_cache

        session_id = "session_123"
        _report_cache[(session_id, False)] = {"cached": True}

        assert (session_id, False) in _report_cache

        _invalidate_cache(session_id)

        assert (session_id, False) not in _report_cache


class TestSessionApproval:
    """Test session approval endpoint."""

    @pytest.fixture
    def mock_repo(self):
        """Mock RuntimeRepository."""
        repo = MagicMock()
        repo.get_session_files.return_value = []
        repo.get_session_payload.return_value = {
            "group": {"features": {"duration_sec": 3600}}
        }
        return repo

    @patch("storage.db.session.SessionLocal")
    @patch("core.tolerance.learn_from_session")
    def test_approve_session(self, mock_learn, mock_session_local, mock_repo):
        """Test approving a session."""
        from api.routes.sessions import approve_session
        from unittest.mock import MagicMock as MockRule

        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_learn.return_value = [MockRule(feature_name="duration_sec")]

        result = approve_session(
            "session_123",
            payload={"confirmed_by": "test_operator"},
            repo=mock_repo,
        )

        assert result["status"] == "approved"
        assert result["session_id"] == "session_123"
        mock_learn.assert_called_once()


class TestSessionIngest:
    """Test session ingestion."""

    @patch("api.routes.sessions.IngestionService")
    @patch("api.routes.sessions.group_files_into_sessions")
    def test_ingest_session(self, mock_group, mock_service):
        """Test ingesting new session."""
        from api.routes.sessions import ingest_session

        mock_service_instance = MagicMock()
        mock_service.return_value = mock_service_instance
        mock_service_instance.parse.return_value = MagicMock(
            root="/test",
            files=[],
            skipped=[],
            diagnostics=[],
        )

        mock_group_instance = MagicMock()
        mock_group_instance.group_id = "session_123"
        mock_group_instance.model_dump = MagicMock(return_value={})
        mock_group_instance.files = []
        mock_group.return_value = [mock_group_instance]

        repo = MagicMock()
        repo.commit = MagicMock()

        payload = {"folder": "/test/folder", "session_id": "session_123"}
        result = ingest_session(payload, repo=repo)

        assert "groups" in result
        repo.flush.assert_called_once()