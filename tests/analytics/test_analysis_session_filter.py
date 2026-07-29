"""Cross-session analysis must look at prints only.

The filter existed as a docstring promise ("Return all REAL_PRINT sessions")
but not as code: the classification was computed and attached to every row,
then never tested. Preparation runs sit at ~21% oxygen because the chamber has
not been purged yet, so including them made the maintenance forecast report
"SO2 past its 2.0 alarm threshold — check the unit now" about ordinary air.
"""
from datetime import datetime, timezone

import pytest

from api.routes.analysis import _load_sessions
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def _add(db, session_id: str, classification: str, so2_mean: float, day: int = 1):
    """A session carrying signal_stats, as the analysis loader expects."""
    db.add(BuildSession(
        session_id=session_id,
        start_ts=datetime(2026, 3, day, tzinfo=timezone.utc),
        classification=classification,
        context={"runtime_payload": {"group": {
            "classification": classification,
            "signal_stats": {"SO2": {"mean": so2_mean, "std": 0.1, "group": "oxygen"}},
        }}},
    ))
    db.flush()


def test_real_print_is_loaded(db):
    _add(db, "s_real", "REAL_PRINT", so2_mean=0.4)
    assert [s["session_id"] for s in _load_sessions(db)] == ["s_real"]


def test_pre_burn_session_is_excluded(db):
    """An unpurged chamber reads like air and is not a print measurement."""
    _add(db, "s_pre", "PRE_BURN_SESSION", so2_mean=21.0)
    assert _load_sessions(db) == []


def test_incomplete_session_is_excluded(db):
    _add(db, "s_unknown", "INCOMPLETE_OR_UNKNOWN", so2_mean=21.0)
    assert _load_sessions(db) == []


def test_resumed_print_still_counts_as_a_print(db):
    _add(db, "s_resume", "REAL_PRINT_WITH_RESUME", so2_mean=0.5)
    assert [s["session_id"] for s in _load_sessions(db)] == ["s_resume"]


def test_air_readings_do_not_reach_the_forecast(db):
    """End to end: the pre-burn oxygen must not drive a maintenance alarm."""
    from analytics.prediction.maintenance import forecast_maintenance

    for day, (sid, cls, so2) in enumerate([
        ("s_p1", "PRE_BURN_SESSION", 21.0),
        ("s_p2", "PRE_BURN_SESSION", 21.0),
        ("s_r1", "REAL_PRINT", 0.4),
        ("s_r2", "REAL_PRINT", 0.5),
    ], start=1):
        _add(db, sid, cls, so2_mean=so2, day=day)

    sessions = _load_sessions(db)
    assert len(sessions) == 2

    forecasts = forecast_maintenance(sessions, {"SO2": {"alarm_high": 2.0}})
    assert not [f for f in forecasts if "уже за порогом" in f["recommendation"]]
