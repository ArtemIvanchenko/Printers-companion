from datetime import datetime, timezone

import pytest

from analytics.prediction import retraining
from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION
from storage.db.session import session_scope
from storage.repositories.model_registry import ModelRegistryRepository


def _candidate(fingerprint: str = "a" * 64) -> dict:
    return {
        "model_name": "defect_risk",
        "algorithm": "logreg",
        "owner_node_id": "pc-a",
        "training_fingerprint": fingerprint,
        "feature_schema_hash": "b" * 64,
        "training_session_ids": ["old-1", "old-2"],
        "training_size": 20,
        "positive_count": 10,
        "artifact": {"type": "logreg", "features": ["readiness"]},
        "metrics": {"cv_auc": 0.8},
        "quality_gates": {"cross_validation_passed": True},
        "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION,
    }


def test_registry_promotes_only_from_shadow_and_archives_previous_active():
    with session_scope() as db:
        repo = ModelRegistryRepository(db)
        first = repo.register_shadow(_candidate())
        assert first["status"] == "shadow"
        assert repo.get_active("defect_risk") is None

        repo.record_shadow_evaluation(
            first["model_version_id"],
            {"candidate": {"sample_size": 8, "roc_auc": 0.9}},
            decision_reason="future validation passed",
        )
        active = repo.promote(first["model_version_id"], "proved better")
        assert active is not None and active["status"] == "active"

        second = repo.register_shadow(_candidate("c" * 64))
        promoted = repo.promote(second["model_version_id"], "proved better again")
        assert promoted is not None
        history = repo.list("defect_risk")
        assert sum(row["status"] == "active" for row in history) == 1
        assert any(row["status"] == "archived" for row in history)


def test_shadow_candidate_waits_for_unseen_labels(monkeypatch):
    shadow = {
        "training_session_ids": ["old"],
        "artifact": {"type": "candidate"},
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    rows = [
        {"session_id": f"new-{index}", "label": index % 2, "group": {"index": index},
         "start_ts": "2026-01-02T00:00:00+00:00", "label_observed_at": "2026-01-03T00:00:00+00:00"}
        for index in range(retraining.MIN_SHADOW_LABELS - 1)
    ]

    def fake_predict(group, model=None):
        return {"method": "model" if model else "heuristic", "risk": 0.8 if group["index"] % 2 else 0.2}

    monkeypatch.setattr(retraining, "predict_defect_risk", fake_predict)
    result = retraining._shadow_evaluation(rows, shadow, None)
    assert result["decision"] == "wait"
    assert result["metrics"]["candidate"]["sample_size"] == len(rows)


def test_shadow_candidate_must_beat_current_method_on_future_labels(monkeypatch):
    shadow = {"training_session_ids": ["old"], "artifact": {"type": "candidate"},
              "created_at": "2026-01-01T00:00:00+00:00"}
    rows = [
        {"session_id": f"new-{index}", "label": index % 2, "group": {"label": index % 2},
         "start_ts": "2026-01-02T00:00:00+00:00", "label_observed_at": "2026-01-03T00:00:00+00:00"}
        for index in range(10)
    ]

    def fake_predict(group, model=None):
        label = group["label"]
        if model:  # candidate: strongly separates future good/defect outcomes
            return {"method": "model", "risk": 0.9 if label else 0.1}
        return {"method": "heuristic", "risk": 0.6 if label else 0.4}

    monkeypatch.setattr(retraining, "predict_defect_risk", fake_predict)
    result = retraining._shadow_evaluation(rows, shadow, None)
    assert result["decision"] == "promote"
    assert result["metrics"]["candidate"]["brier"] < result["metrics"]["comparator"]["brier"]


def test_backfilled_old_prints_cannot_promote_shadow_model(monkeypatch):
    shadow = {"training_session_ids": [], "artifact": {"type": "candidate"},
              "created_at": "2026-06-01T00:00:00+00:00"}
    rows = [{"session_id": f"old-backfill-{i}", "label": i % 2, "group": {"label": i % 2},
             "start_ts": "2026-05-01T00:00:00+00:00", "label_observed_at": "2026-07-01T00:00:00+00:00"}
            for i in range(10)]
    monkeypatch.setattr(retraining, "predict_defect_risk", lambda group, model=None: {
        "method": "model" if model else "heuristic",
        "risk": (0.9 if group["label"] else 0.1) if model else 0.5,
    })
    result = retraining._shadow_evaluation(rows, shadow, None)
    assert result["decision"] == "wait"
    assert result["metrics"]["candidate"]["sample_size"] == 0
    assert result["metrics"]["temporal_exclusions"]["not_a_future_print"] == 10


def test_shadow_without_temporal_boundary_cannot_claim_future_validation(monkeypatch):
    monkeypatch.setattr(retraining, "predict_defect_risk", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("A candidate without a cutoff must not evaluate alleged future labels"),
    ))
    result = retraining._shadow_evaluation([
        {"session_id": "new", "label": 1, "group": {}},
    ], {"training_session_ids": [], "artifact": {"type": "candidate"}}, None)
    assert result["decision"] == "wait"
    assert result["metrics"]["temporal_exclusions"]["candidate_time_unavailable"] == 1


@pytest.mark.parametrize("changes,reason", [
    ({"start_ts": None}, "print_time_unavailable"),
    ({"start_ts": "2026-01-01T00:00:00Z"}, "not_a_future_print"),
    ({"label_observed_at": None}, "label_time_unavailable"),
    ({"label_observed_at": "2026-01-01T00:00:00Z"}, "not_a_future_label"),
    ({"label_observed_at": "2026-01-01T12:00:00Z"}, "not_a_future_label"),
])
def test_shadow_rejects_missing_or_contradictory_temporal_evidence(changes, reason):
    row = {"session_id": "new", "label": 1, "group": {},
           "start_ts": "2026-01-02T00:00:00Z", "label_observed_at": "2026-01-03T00:00:00Z",
           **changes}
    result = retraining._shadow_evaluation([row], {
        "training_session_ids": [], "artifact": {}, "created_at": "2026-01-01T00:00:00Z",
    }, None)
    assert result["decision"] == "wait"
    assert result["metrics"]["temporal_exclusions"][reason] == 1


def test_shadow_does_not_compare_against_in_sample_champion_answers():
    result = retraining._shadow_evaluation([{
        "session_id": "known-to-champion", "label": 1, "group": {},
        "start_ts": "2026-01-02T00:00:00Z", "label_observed_at": "2026-01-03T00:00:00Z",
    }], {"created_at": "2026-01-01T00:00:00Z"}, {
        "training_session_ids": ["known-to-champion"],
    })
    assert result["decision"] == "wait"
    assert result["metrics"]["temporal_exclusions"]["comparator_training_sample"] == 1


def test_training_snapshot_preserves_label_observation_time_and_identity():
    from domain.models.quality import QualityOutcome
    from domain.models.sessions import BuildSession

    when = datetime(2026, 6, 2, tzinfo=timezone.utc)
    with session_scope() as db:
        db.add(BuildSession(session_id="label-time-session", classification="REAL_PRINT",
                            start_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
                            context={"runtime_payload": {"group": {
                                "features": {"layers": 100},
                                "analysis_snapshot": {"schema_version": 1, "analysis_id": "test-analysis",
                                                      "provenance": {"analysis_version": ANALYSIS_VERSION},
                                                      "features": {"layers": 100}, "health": {},
                                                      "data_quality": {}, "signal_stats": {}},
                            }}}))
        db.flush()
        db.add(QualityOutcome(outcome_id="label-time-outcome", session_id="label-time-session",
                              timestamp=when, inspection_type="visual", result="accepted", is_final=True))
        db.flush()
        prepared = retraining.prepare_retraining(db)
        row = next(row for row in prepared["rows"] if row["session_id"] == "label-time-session")
        assert row["label"] == 0
        assert row["label_outcome_id"] == "label-time-outcome"
        assert row["label_observed_at"] == when.isoformat()


def test_calculation_registers_valid_model_as_shadow(monkeypatch):
    rows = [
        {
            "session_id": f"s-{index}",
            "start_ts": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            "label": index % 2,
            "group": {"features": {"atmosphere_readiness": 90 - index}},
        }
        for index in range(20)
    ]
    monkeypatch.setattr(
        retraining,
        "train_defect_model",
        lambda data: {
            "type": "logreg",
            "features": ["readiness"],
            "cv_auc": 0.81,
            "cv_folds": 3,
            "n_train": len(data),
            "n_defects": 10,
        },
    )
    result = retraining.calculate_retraining(
        {"rows": rows, "shadow": None, "active": None}, owner_node_id="pc-a"
    )
    assert result["candidate"] is not None
    assert result["candidate"]["quality_gates"]["future_shadow_validation_required"] is True
    assert result["candidate"]["training_size"] == 20


def test_outdated_active_artifact_is_not_applied_or_deleted():
    with session_scope() as db:
        repo = ModelRegistryRepository(db)
        candidate = _candidate()
        candidate["analysis_version"] = "0.4.6"
        old = repo.register_shadow(candidate)
        repo.promote(old["model_version_id"], "historical validation")
        assert retraining.active_model(db) is None
        assert repo.get_active("defect_risk")["model_version_id"] == old["model_version_id"]


def test_old_shadow_is_rejected_without_claiming_new_validation(monkeypatch):
    monkeypatch.setattr(retraining, "_shadow_evaluation", lambda *a: pytest.fail("old feature semantics"))
    result = retraining.calculate_retraining(
        {"rows": [], "shadow": {**_candidate(), "analysis_version": "0.4.6"}}, owner_node_id="pc-a",
    )
    assert result["evaluation"]["decision"] == "reject"
    assert result["evaluation"]["metrics"]["feature_version_compatible"] is False


def test_legacy_labelled_session_is_retained_but_excluded_from_new_training():
    from domain.models.quality import QualityOutcome
    from domain.models.sessions import BuildSession
    with session_scope() as db:
        db.add(BuildSession(session_id="legacy", classification="REAL_PRINT",
                            context={"runtime_payload": {"group": {"features": {"layers": 50}}}}))
        db.flush()
        db.add(QualityOutcome(outcome_id="old-label", session_id="legacy", inspection_type="visual",
                              timestamp=datetime(2026, 6, 2, tzinfo=timezone.utc),
                              result="accepted", is_final=True))
        db.flush()
        assert retraining.prepare_retraining(db)["rows"] == []
        assert db.get(QualityOutcome, "old-label") is not None
