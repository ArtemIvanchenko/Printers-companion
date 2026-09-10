from datetime import datetime, timezone

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
    }
    rows = [
        {"session_id": f"new-{index}", "label": index % 2, "group": {"index": index}}
        for index in range(retraining.MIN_SHADOW_LABELS - 1)
    ]

    def fake_predict(group, model=None):
        return {"method": "model" if model else "heuristic", "risk": 0.8 if group["index"] % 2 else 0.2}

    monkeypatch.setattr(retraining, "predict_defect_risk", fake_predict)
    result = retraining._shadow_evaluation(rows, shadow, None)
    assert result["decision"] == "wait"
    assert result["metrics"]["candidate"]["sample_size"] == len(rows)


def test_shadow_candidate_must_beat_current_method_on_future_labels(monkeypatch):
    shadow = {"training_session_ids": ["old"], "artifact": {"type": "candidate"}}
    rows = [
        {"session_id": f"new-{index}", "label": index % 2, "group": {"label": index % 2}}
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
