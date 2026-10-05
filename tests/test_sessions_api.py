import pytest
from unittest.mock import MagicMock, patch


class TestSessionsAPI:
    """Test API sessions routes."""

    @pytest.fixture
    def mock_repo(self):
        """Mock RuntimeRepository."""
        repo = MagicMock()
        repo.get_session_payload.return_value = None
        repo.list_session_payloads.return_value = []
        repo.commit = MagicMock()
        return repo

    def test_list_sessions_empty(self, mock_repo):
        """Test listing sessions when none exist (paginated contract)."""
        from api.routes.sessions import list_sessions

        with patch("domain.services.session_reports.list_sessions", return_value={"items": [], "total": 0}) as read:
            result = list_sessions(repo=mock_repo)
        read.assert_called_once_with(mock_repo.db, skip=0, limit=100)

        assert result["items"] == []
        assert result["total"] == 0

    def test_list_sessions_with_data(self, mock_repo):
        """Test listing sessions with data (paginated contract)."""
        from api.routes.sessions import list_sessions

        with patch("domain.services.session_reports.list_sessions", return_value={
            "total": 2, "items": [{"session_id": "session_1"}, {"session_id": "session_2"}],
        }):
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
        repo.commit = MagicMock()
        return repo

    @patch("domain.services.session_reports.read_report")
    def test_generate_report_new(self, mock_read, mock_repo):
        """The compatibility renderer reads, never executes another analysis."""
        from api.routes.sessions import _generate_report

        mock_read.return_value = {
            "report_id": "report_123",
            "session_id": "session_123",
            "timeline": [],
            "phase_segments": [],
            "file_inventory": [],
            "data_quality": {"parse_diagnostics": []},
        }

        result = _generate_report("session_123", include_markdown=False, repo=mock_repo)

        assert result["report_id"] == "report_123"
        mock_read.assert_called_once_with(mock_repo.db, "session_123", include_markdown=False)
        mock_repo.flush.assert_not_called()

    @patch("domain.services.session_reports.read_report")
    def test_generate_report_with_markdown(self, mock_read, mock_repo):
        """Test generating report with markdown."""
        from api.routes.sessions import _generate_report

        mock_read.return_value = {
            "report_id": "report_123",
            "session_id": "session_123",
            "timeline": [],
            "phase_segments": [],
            "file_inventory": [],
            "data_quality": {"parse_diagnostics": []},
            "markdown": "# Report",
        }

        result = _generate_report("session_123", include_markdown=True, repo=mock_repo)
        assert "markdown" in result
        mock_read.assert_called_once_with(mock_repo.db, "session_123", include_markdown=True)

    def test_report_cache(self):
        """Old invalidation callers remain safe; stale process cache is gone."""
        from api.routes import sessions

        assert not hasattr(sessions, "_report_cache")
        assert sessions._invalidate_cache("session_123") is None


class TestSessionApproval:
    """Test session approval endpoint."""

    @pytest.fixture
    def mock_repo(self):
        """Mock RuntimeRepository."""
        repo = MagicMock()
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

    @patch("domain.services.session_requests.request_ingest")
    def test_ingest_session(self, mock_request):
        """The HTTP adapter returns the durable-v2 request, not parse output."""
        from api.routes.sessions import ingest_session

        mock_request.return_value = {"contract_version": 2, "job_id": "import_123", "groups": []}
        repo = MagicMock()
        payload = {"folder": "/test/folder"}
        result = ingest_session(payload, repo=repo)
        assert result["job_id"] == "import_123"
        mock_request.assert_called_once()
        assert mock_request.call_args.args == (repo.db, payload)
        repo.flush.assert_not_called()
