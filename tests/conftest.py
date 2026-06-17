import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """Create the database schema once per test session.

    Integration tests that hit a real (sqlite) engine need the tables to exist.
    In production the API lifespan / Alembic migrations handle this, but the test
    harness bypasses both, so create_all() here gives a stable baseline.
    """
    from storage.db.session import engine
    if engine.url.get_backend_name() == "sqlite":
        from storage.db.init_db import create_all
        create_all()
    yield


@pytest.fixture(autouse=True)
def _clean_db(_ensure_schema):
    """Empty every table before each test.

    The harness uses a shared sqlite *file* (NullPool can't share an in-memory
    DB across connections), so rows committed by one test persist into the
    next. Tests that insert fixed ids (e.g. ``s_cand_a``) would then collide on
    UNIQUE constraints on a rerun or full-suite run. Wiping tables per test
    keeps them isolated and idempotent.
    """
    from storage.db.session import engine
    if engine.url.get_backend_name() != "sqlite":
        yield
        return
    from storage.db.base import Base
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture(autouse=True)
def _stub_object_store(monkeypatch):
    """No MinIO in the test harness: make the real ObjectStore report unavailable
    so report offload short-circuits without a slow network round-trip. Tests that
    exercise the offload path inject their own in-memory ObjectStore."""
    try:
        monkeypatch.setattr(
            "storage.object_store.minio_client.ObjectStore.is_available",
            lambda self: False,
            raising=False,
        )
    except Exception:
        pass  # minio not installed -> offload import fails fast anyway


@pytest.fixture
def mock_db_session():
    """Mock SQLAlchemy session for testing."""
    session = MagicMock()
    session.commit = MagicMock()
    session.rollback = MagicMock()
    session.close = MagicMock()
    session.get = MagicMock(return_value=None)
    session.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    session.scalar = MagicMock(return_value=None)
    return session


@pytest.fixture
def sample_operator_event():
    """Sample operator event data."""
    return {
        "event_id": f"op_event_{uuid4().hex[:8]}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "created_by": "test_operator",
        "source_channel": "telegram",
        "event_type": "gas_consumption_recorded",
        "value": "150",
        "unit": "bar",
    }


@pytest.fixture
def sample_session_payload():
    """Sample session payload."""
    return {
        "files": [],
        "group": {
            "group_id": f"session_{uuid4().hex[:8]}",
            "confidence": 0.95,
            "features": {
                "material": "AlSi10Mg",
                "duration_sec": 3600.0,
                "gas_cylinder_id": "A1",
            },
        },
    }


@pytest.fixture
def sample_report():
    """Sample report data."""
    return {
        "report_id": f"report_{uuid4().hex[:8]}",
        "session_id": f"session_{uuid4().hex[:8]}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timeline": [],
        "phase_segments": [],
        "file_inventory": [],
        "data_quality": {"parse_diagnostics": []},
    }


@pytest.fixture
def sample_operator_text_gas():
    """Sample operator text for gas events."""
    return "Баллон A1 заменён, потрачено 150 бар"


@pytest.fixture
def sample_operator_text_powder():
    """Sample operator text for powder events."""
    return "Порошок batch123, использовано 2.5 кг"


@pytest.fixture
def sample_operator_text_defect():
    """Sample operator text for defect events."""
    return "Дефект porosity, забраковано"