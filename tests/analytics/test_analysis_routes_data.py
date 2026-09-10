"""Data-selection guards for analytics API endpoints."""
from datetime import datetime, timedelta, timezone

from api.routes.analysis import (
    _load_quality_labels,
    _load_session_groups,
    _training_fingerprint,
)
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal


def _group(classification: str, readiness: float = 90.0) -> dict:
    return {
        "classification": classification,
        "features": {"atmosphere_readiness": readiness},
        "health": {},
        "signal_stats": {},
    }


def test_defect_risk_loads_only_real_prints():
    with SessionLocal() as db:
        for idx, classification in enumerate(("REAL_PRINT", "PRE_BURN_SESSION", "SERVICE_SESSION")):
            db.add(BuildSession(
                session_id=f"s_{idx}", classification=classification,
                start_ts=datetime(2027, 1, idx + 1, tzinfo=timezone.utc),
                context={"runtime_payload": {"group": _group(classification)}},
            ))
        db.flush()

        sessions = _load_session_groups(db)

    assert [session["session_id"] for session in sessions] == ["s_0"]


def test_latest_quality_outcome_wins_deterministically():
    start = datetime(2027, 2, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        db.add(BuildSession(session_id="s_quality", start_ts=start))
        # Insert newest first to prove row/insertion order is irrelevant.
        db.add(QualityOutcome(
            outcome_id="q_new", session_id="s_quality", timestamp=start + timedelta(days=1),
            inspection_type="visual", result="rejected", is_final=True,
        ))
        db.add(QualityOutcome(
            outcome_id="q_old", session_id="s_quality", timestamp=start,
            inspection_type="visual", result="accepted", is_final=True,
        ))
        db.flush()

        labels = _load_quality_labels(db)

    assert labels["s_quality"] == 1


def test_generic_quality_observation_is_not_ml_ground_truth():
    start = datetime(2027, 2, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        db.add(BuildSession(session_id="s_observation", start_ts=start))
        db.add(QualityOutcome(
            outcome_id="q_observation",
            session_id="s_observation",
            timestamp=start,
            inspection_type="visual",
            result="rejected",
            is_final=False,
        ))
        db.flush()
        labels = _load_quality_labels(db)

    assert "s_observation" not in labels


def test_model_cache_fingerprint_includes_feature_values():
    first = {
        "session_id": "s1", "start_ts": "2027-01-01T00:00:00+00:00",
        "group": _group("REAL_PRINT", readiness=90.0),
    }
    changed = {
        **first,
        "group": _group("REAL_PRINT", readiness=20.0),
    }
    assert _training_fingerprint([(first, 0)]) != _training_fingerprint([(changed, 0)])
