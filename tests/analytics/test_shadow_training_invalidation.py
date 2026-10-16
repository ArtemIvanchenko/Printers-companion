"""Regressions for serial correction of shadow training inputs."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from analytics.prediction import retraining
from core.config.settings import get_settings
from core.versioning.constants import ANALYSIS_VERSION
from domain.models.jobs import BackgroundJob
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from domain.services.print_cards import quality
from storage.db.session import session_scope
from storage.repositories.model_registry import ModelRegistryRepository
from storage.repositories.prints_repo import PrintsRepository
from worker import model_tasks


def _current_group(layers):
    return {"features": {"layers": layers}, "analysis_snapshot": {
        "schema_version": 1, "analysis_id": "shadow-training-test",
        "provenance": {"analysis_version": ANALYSIS_VERSION},
        "features": {"layers": layers}, "health": {},
        "data_quality": {}, "signal_stats": {},
    }}


def test_serial_qc_correction_rejects_old_shadow_and_trains_new_candidate(monkeypatch):
    """Real QC/SQL/job/registry workflow; CPU artifact is an explicit double."""
    node = "shadow-training-pc"
    monkeypatch.setattr(get_settings(), "compute_node_id", node)
    with session_scope() as db:
        cards = []
        for index in range(20):
            sid = f"shadow-training-{index:02d}"
            db.add(BuildSession(
                session_id=sid, origin_compute_node_id=node,
                classification="REAL_PRINT",
                start_ts=datetime(2026, 5, index + 1, tzinfo=timezone.utc),
                context={"runtime_payload": {"group": _current_group(100 + index)}},
            ))
            db.flush()
            card = PrintsRepository(db).create_print_record({
                "name": sid, "origin_compute_node_id": node, "session_id": sid,
            })
            cards.append(card)
            if index < 19:
                db.add(QualityOutcome(
                    outcome_id=f"seed-{sid}", print_record_id=card["record_id"],
                    session_id=sid, timestamp=datetime(2026, 5, 21, tzinfo=timezone.utc),
                    inspection_type="visual", result="rejected" if index % 2 else "accepted",
                    is_final=True, inspection_result="Unchanged synthetic strict fixture",
                    defect_type="porosity" if index % 2 else None,
                    defect_location="body" if index % 2 else None,
                ))
        target = cards[-1]
    with session_scope() as db:
        accepted = quality.create_quality_outcome(
            PrintsRepository(db), target["record_id"],
            {"result": "accepted", "inspection_result": "Synthetic initial inspection"},
            actor=node,
        )

    calls = []

    def cpu_artifact(data):
        calls.append([int(label) for _, label in data])
        return {"type": "logreg", "features": ["layers"], "mean": [100.0],
                "scale": [1.0], "coef": [0.0], "intercept": 0.0,
                "cv_auc": 0.9, "cv_folds": 3, "n_train": len(data),
                "n_defects": sum(label for _, label in data)}

    monkeypatch.setattr(retraining, "train_defect_model", cpu_artifact)
    assert model_tasks.process_next_model_task("shadow-training-first", owner_node_id=node)
    with session_scope() as db:
        old = ModelRegistryRepository(db).get_shadow("defect_risk", include_artifact=True)
        old_artifact = deepcopy(old["artifact"])
        assert old["training_size"] == 20 and old["positive_count"] == 9
        assert db.get(BackgroundJob, accepted["model_retraining_job_id"]).status == "done"

    # Strictly serial: the original candidate is committed before correction.
    with session_scope() as db:
        corrected = quality.create_quality_outcome(
            PrintsRepository(db), target["record_id"],
            {"result": "rejected", "inspection_result": "Synthetic corrected inspection",
             "defect_type": "porosity", "defect_location": "body",
             "supersedes_outcome_id": accepted["outcome_id"]},
            actor=node,
        )
    assert model_tasks.process_next_model_task("shadow-training-second", owner_node_id=node)
    with session_scope() as db:
        registry = ModelRegistryRepository(db)
        retained = registry.get(old["model_version_id"], include_artifact=True)
        current = registry.get_shadow("defect_risk", include_artifact=True)
        fresh = retraining.prepare_retraining(db)["rows"]
        job = db.get(BackgroundJob, corrected["model_retraining_job_id"])
        assert retained["status"] == "rejected"
        assert retained["artifact"] == old_artifact
        assert retained["training_fingerprint"] == old["training_fingerprint"]
        assert current["model_version_id"] != old["model_version_id"]
        assert current["status"] == "shadow" and current["positive_count"] == 10
        assert current["training_fingerprint"] == retraining.training_fingerprint(fresh)
        assert current["training_size"] == 20
        assert len(calls) == 2 and sum(calls[0]) == 9 and sum(calls[1]) == 10
        assert job.status == "done" and job.result_json["evaluation"]["decision"] == "reject"
        assert registry.get_active("defect_risk") is None


def _training_rows():
    return [{"session_id": f"known-{index}", "label": index,
             "label_outcome_id": f"outcome-{index}",
             "label_observed_at": "2026-01-02T00:00:00+00:00",
             "start_ts": "2026-01-01T00:00:00+00:00", "group": _current_group(100 + index)}
            for index in range(2)]


def _shadow_for(rows):
    return {"model_version_id": "original-shadow", "analysis_version": ANALYSIS_VERSION,
            "training_session_ids": [row["session_id"] for row in rows],
            "training_fingerprint": retraining.training_fingerprint(rows),
            "created_at": "2026-02-01T00:00:00+00:00", "artifact": {"type": "logreg"}}


def test_new_future_row_preserves_shadow_and_original_fingerprint_order(monkeypatch):
    original = _training_rows()
    shadow = _shadow_for(original)
    future = {"session_id": "future", "label": 1, "group": _current_group(200),
              "label_outcome_id": "future-outcome",
              "start_ts": "2026-02-02T00:00:00+00:00",
              "label_observed_at": "2026-02-03T00:00:00+00:00"}
    seen = []
    monkeypatch.setattr(retraining, "_shadow_evaluation", lambda rows, candidate, active: (
        seen.append((rows, candidate)) or {"decision": "wait", "reason": "Future evidence pending", "metrics": {}}
    ))
    monkeypatch.setattr(retraining, "train_defect_model", lambda data: pytest.fail("New future labels must not reset shadow"))
    # Current rows may be reordered; saved training IDs define fingerprint order.
    rows = [future, *reversed(original)]
    result = retraining.calculate_retraining({"rows": rows, "shadow": shadow}, owner_node_id="pc")
    assert result["evaluation"]["decision"] == "wait"
    assert result["candidate"] is None and len(seen) == 1
    assert seen[0][1] is shadow


@pytest.mark.parametrize("change", ["label", "outcome_id", "observation_time", "feature", "missing_row"])
def test_changed_original_training_evidence_rejects_before_future_scoring(monkeypatch, change):
    original = _training_rows()
    shadow = _shadow_for(original)
    current = deepcopy(original)
    if change == "label":
        current[0]["label"] = 1
    elif change == "outcome_id":
        current[0]["label_outcome_id"] = "corrected-outcome"
    elif change == "observation_time":
        current[0]["label_observed_at"] = "2026-02-04T00:00:00+00:00"
    elif change == "feature":
        current[0]["group"] = _current_group(500)
    else:
        current.pop(0)
    calls = []
    monkeypatch.setattr(retraining, "_shadow_evaluation", lambda *args: pytest.fail("Invalid shadow must not score future labels"))
    monkeypatch.setattr(retraining, "train_defect_model", lambda data: calls.append(data))
    result = retraining.calculate_retraining({"rows": current, "shadow": shadow}, owner_node_id="pc")
    assert result["evaluation"]["decision"] == "reject"
    assert result["evaluation"]["metrics"] == {}
    assert result["candidate"] is None and len(calls) == 1
    assert len(calls[0]) == len(current)
