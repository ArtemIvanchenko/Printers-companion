"""Regression contracts for owner-local, snapshot-fenced time calibration."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from analytics.prediction import calibration
from analytics.prediction.calibration_inputs import load_calibration_inputs
from api.main import app
from core.config.settings import get_settings
from domain.models.events import LayerSnapshot
from domain.models.jobs import BackgroundJob
from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession
from storage.db.session import engine, session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker import calibration_tasks


@pytest.fixture
def history():
    node = get_settings().compute_node_id
    with session_scope() as db:
        db.add(MachineParams(id=1, correction_locked=False, recoat_time_by_mat={"steel": 3000}))
        for i in range(3):
            sid = f"cal-session-{i}"
            start = datetime(2026, 1, 1 + i, tzinfo=timezone.utc)
            db.add(BuildSession(session_id=sid, origin_compute_node_id=node,
                                start_ts=start, end_ts=start + timedelta(hours=10),
                                classification="REAL_PRINT"))
            db.add(PrintRecord(
                record_id=f"cal-record-{i}", name=f"Печать {i}", session_id=sid,
                origin_compute_node_id=node, material="steel", layer_thickness_mm=0.06,
                metadata_json={"session_link_confirmed": True, "prediction": {
                    "input_revision": 1, "build_origin_source": "explicit", "build_origin_z_mm": 0,
                    "raw_print_hours": 9, "raw_scan_hours": 8.86, "raw_recoat_hours": 0.14,
                    "print_hours": 9, "material": "steel", "layer_thickness_mm": 0.06,
                    "layer_count": 100, "scan_source": "physics",
                }},
            ))
            db.add_all([LayerSnapshot(session_id=sid, layer=layer,
                                      features={"burn_ms": 355000, "pour_ms": 5000, "make_layer_ms": 360500})
                        for layer in range(1, 101)])
        job = calibration.enqueue_calibration(db, owner_node_id=node, trigger="test", event_id="first")
    return job


def test_http_enqueues_instead_of_fitting(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP must not fit calibration or open raw files")

    monkeypatch.setattr("analytics.prediction.accuracy.recalibrate_and_apply", forbidden)
    monkeypatch.setattr("analytics.prediction.scan_calibration.scan_calibration_report", forbidden)
    client = TestClient(app)
    try:
        response = client.post("/prints/recalibrate")
        assert response.status_code == 202
        assert response.json()["contract_version"] == 2
        assert response.json()["status"] == "pending"
        job = client.get(f"/background-analysis/jobs/{response.json()['job_id']}").json()
        assert job["owner_node_id"] == get_settings().compute_node_id
        assert client.get("/prints/prediction-accuracy").json()["scan"]["status"] == "not_calculated"
    finally:
        client.close()


def test_pending_coalesces_but_new_event_during_fit_is_not_dropped(history):
    node = get_settings().compute_node_id
    with session_scope() as db:
        assert calibration.enqueue_calibration(db, owner_node_id=node, trigger="again")["job_id"] == history["job_id"]
        JobsRepository(db).claim_next(calibration.JOB_TYPE, owner_node_id=node, lease_owner="one")
        following = calibration.enqueue_calibration(db, owner_node_id=node, trigger="during-fit")
        assert following["job_id"] != history["job_id"]
        assert calibration.enqueue_calibration(db, owner_node_id=node, trigger="again-again")["job_id"] == following["job_id"]


def test_different_pc_cannot_claim(history):
    assert calibration_tasks.process_next_calibration_task("other-worker", "other-pc") is False
    with session_scope() as db:
        assert JobsRepository(db).get(history["job_id"])["status"] == "pending"


def test_all_math_runs_without_nas_connection_and_result_is_persisted(history, monkeypatch):
    checked_out = []

    def checkout(*args):
        checked_out.append(True)

    def checkin(*args):
        checked_out.pop()

    original = calibration_tasks.calculate_calibration

    def calculate(*args, **kwargs):
        assert not checked_out
        result = original(*args, **kwargs)
        assert not checked_out
        return result

    monkeypatch.setattr(calibration_tasks, "calculate_calibration", calculate)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        assert calibration_tasks.process_next_calibration_task("worker")
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    with session_scope() as db:
        job = JobsRepository(db).get(history["job_id"])
        assert job["status"] == "done", job["error"]
        assert job["result"]["status"] == "published"
        assert len(job["result"]["input_fingerprint"]) == 64
        assert job["result"]["provenance"]["generated_by"] == get_settings().compute_node_id
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 5000}
        assert db.get(MachineParams, 1).time_correction_by_mat["steel@0.060"] > 1
        assert calibration.latest_calibration_report(db)["scan_report"]["n_records"] == 3


@pytest.mark.parametrize("change", ["record", "timing", "params", "lock", "new_pair"])
def test_input_or_settings_edit_discards_calculation_and_retries(history, monkeypatch, change):
    original = calibration_tasks.calculate_calibration

    def calculate(inputs, **kwargs):
        result = original(inputs, **kwargs)
        with session_scope() as db:
            if change == "record":
                db.get(PrintRecord, "cal-record-0").material = "aluminum"
            elif change == "timing":
                row = db.scalar(select(LayerSnapshot).limit(1))
                row.features = {**row.features, "pour_ms": 9000}
            elif change == "params":
                db.get(MachineParams, 1).recoat_time_by_mat = {"steel": 7000}
            elif change == "lock":
                db.get(MachineParams, 1).correction_locked = True
            else:
                db.add(BuildSession(session_id="new-session", classification="REAL_PRINT"))
                db.add(PrintRecord(record_id="new-record", name="Новая", session_id="new-session"))
        return result

    monkeypatch.setattr(calibration_tasks, "calculate_calibration", calculate)
    calibration_tasks.process_next_calibration_task("worker")
    with session_scope() as db:
        job = JobsRepository(db).get(history["job_id"])
        assert job["status"] == "pending"
        assert "изменились" in job["error"]
        assert not db.get(MachineParams, 1).time_correction_by_mat
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 7000 if change == "params" else 3000}


@pytest.mark.parametrize("change", ["expired", "generation"])
def test_stale_lease_cannot_publish(history, monkeypatch, change):
    original = calibration_tasks.calculate_calibration

    def calculate(*args, **kwargs):
        result = original(*args, **kwargs)
        with session_scope() as db:
            row = db.get(BackgroundJob, history["job_id"])
            if change == "expired":
                row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            else:
                row.lease_generation += 1
        return result

    monkeypatch.setattr(calibration_tasks, "calculate_calibration", calculate)
    calibration_tasks.process_next_calibration_task("worker")
    with session_scope() as db:
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 3000}
        assert db.get(BackgroundJob, history["job_id"]).status == "running"


def test_publish_failure_rolls_back_completion_and_all_params(history, monkeypatch):
    original = calibration_tasks.publish_calibration

    def failing(db, calculated, **kwargs):
        original(db, calculated, **kwargs)
        db.flush()
        raise RuntimeError("Injected failure before commit")

    monkeypatch.setattr(calibration_tasks, "publish_calibration", failing)
    calibration_tasks.process_next_calibration_task("worker")
    with session_scope() as db:
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 3000}
        assert db.get(BackgroundJob, history["job_id"]).status == "pending"


def test_snapshot_report_matches_legacy_math_and_never_rehydrates(history, monkeypatch):
    from analytics.prediction.accuracy import prediction_accuracy
    from analytics.prediction.recoat_calibration import recoat_accuracy
    from analytics.prediction.scan_calibration import scan_calibration_report

    with session_scope() as db:
        legacy = [prediction_accuracy(db), recoat_accuracy(db), scan_calibration_report(db)]
        inputs = load_calibration_inputs(db)

    def forbidden(*args, **kwargs):
        pytest.fail("Detached calibration must not read local files")

    monkeypatch.setattr("storage.repositories.runtime.RuntimeRepository.get_session_files", forbidden)
    detached = [prediction_accuracy(inputs=inputs), recoat_accuracy(inputs=inputs), scan_calibration_report(inputs=inputs)]
    for reports in (legacy, detached):
        for models in (reports[2]["candidates"], reports[2]["cycle_candidates"]):
            for model in models.values():
                model.pop("fitted_at", None)
    assert detached == legacy
    empty = type(inputs)(inputs.linked, [], inputs.params)
    report = calibration.accuracy_view(empty, None)
    assert len(report["missing_timing_sessions"]) == 3
    assert report["pairs"][0]["actual_hours"] is None


def test_stale_cached_candidates_are_not_presented_as_current(history):
    with session_scope() as db:
        inputs = load_calibration_inputs(db)
    old = {"input_fingerprint": "old", "scan_report": {"candidates": {"steel": {"r2": 0.99}}}}
    result = calibration.accuracy_view(inputs, old)
    assert result["scan"]["status"] == "stale"
    assert result["scan"]["candidates"] == {}


def test_previous_analysis_version_requires_new_fit(history):
    with session_scope() as db:
        inputs = load_calibration_inputs(db)
    old = {"input_fingerprint": inputs.input_fingerprint, "contract_version": 2,
           "provenance": {"analysis_version": "old"},
           "scan_report": {"candidates": {"steel": {"r2": 0.99}}}}
    assert calibration.accuracy_view(inputs, old)["scan"]["status"] == "stale"


def test_lock_wait_is_followed_by_a_fresh_lease_check(history, monkeypatch):
    original = calibration.load_calibration_inputs

    def load(db, **kwargs):
        inputs = original(db, **kwargs)
        # Equivalent to lease expiry while waiting for publication row locks.
        db.get(BackgroundJob, history["job_id"]).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.flush()
        return inputs

    monkeypatch.setattr(calibration, "load_calibration_inputs", load)
    calibration_tasks.process_next_calibration_task("worker")
    with session_scope() as db:
        assert db.get(BackgroundJob, history["job_id"]).status == "running"
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 3000}


def test_locked_params_skip_model_fit(history, monkeypatch):
    with session_scope() as db:
        db.get(MachineParams, 1).correction_locked = True

    def forbidden(*args, **kwargs):
        pytest.fail("Locked calibration must not spend CPU fitting a model")

    monkeypatch.setattr(calibration, "scan_calibration_report", forbidden)
    calibration_tasks.process_next_calibration_task("worker")
    with session_scope() as db:
        assert db.get(BackgroundJob, history["job_id"]).result_json["status"] == "locked"
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 3000}


def test_nas_outage_keeps_owner_job_retryable_without_spending_attempt(history, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def offline(*args, **kwargs):
        raise OperationalError("read", {}, ConnectionError("NAS offline"))

    monkeypatch.setattr(calibration_tasks, "load_calibration_inputs", offline)
    calibration_tasks.process_next_calibration_task("worker")
    with session_scope() as db:
        row = db.get(BackgroundJob, history["job_id"])
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.owner_node_id == get_settings().compute_node_id


def test_correcting_and_unlinking_cards_queue_refresh_without_fit(history, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Card writes must only enqueue calibration")

    monkeypatch.setattr("analytics.prediction.accuracy.recalibrate_and_apply", forbidden)
    client = TestClient(app)
    with session_scope() as db:
        # No pending job from the fixture, so this tests the PATCH trigger.
        db.delete(db.get(BackgroundJob, history["job_id"]))
    for values in ({"material": "aluminum"}, {"session_id": None}):
        current = client.get("/prints/cal-record-0").json()
        response = client.patch("/prints/cal-record-0", json={**values, "expected_revision": current["revision"]})
        assert response.status_code == 200, response.text
    with session_scope() as db:
        jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.job_type == calibration.JOB_TYPE)).all()
        assert len(jobs) == 1
        assert jobs[0].owner_node_id == get_settings().compute_node_id
    client.close()


def test_report_json_is_bounded():
    report = {"records": [{"record_id": str(i)} for i in range(500)],
              "candidates": {str(i): {"source_records": list(range(500))} for i in range(500)},
              "cycle_candidates": {}}
    compact = calibration._compact_scan_report(report)
    assert compact["n_records"] == 500
    assert len(compact["records"]) == len(compact["candidates"]) == 100
    assert len(compact["candidates"]["0"]["source_records"]) == 100
    assert len(report["records"]) == 500


def test_actual_nnls_fit_uses_detached_geometry_and_keeps_provenance(history, monkeypatch):
    from sqlalchemy import delete
    from analytics.prediction import scan_calibration
    from tests.analytics.test_scan_calibration import _series, _snapshot_geometry, _burn_seconds, N_LAYERS

    with session_scope() as db:
        db.execute(delete(LayerSnapshot))
        for i in range(3):
            series = _series(1 + i * 0.4)
            row = db.get(PrintRecord, f"cal-record-{i}")
            metadata = dict(row.metadata_json)
            metadata["prediction"] = {**metadata["prediction"],
                "scan_geometry": _snapshot_geometry(series), "layer_thickness_mm": 0.1,
                "laser_count": 2, "layer_count": N_LAYERS,
                "input_revision": row.revision + 1, "geometry_fingerprint": f"geometry-{i}"}
            row.metadata_json = metadata
            db.add_all([LayerSnapshot(session_id=row.session_id, layer=layer,
                features={"burn_ms": _burn_seconds(series, layer) * 1000, "pour_ms": 5000,
                          "make_layer_ms": _burn_seconds(series, layer) * 1000 + 5500})
                for layer in range(1, N_LAYERS + 1)])
    original = scan_calibration._fit
    fit_calls = []

    def fit(*args, **kwargs):
        # A real NNLS + geometry-grouped holdout runs, not a mocked model.
        fit_calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(scan_calibration, "_fit", fit)
    calibration_tasks.process_next_calibration_task("worker")
    assert fit_calls
    with session_scope() as db:
        job = db.get(BackgroundJob, history["job_id"])
        assert job.status == "done", job.error
        models = db.get(MachineParams, 1).scan_model_by_mat
        assert models, job.result_json
        model = next(iter(models.values()))
        assert model["r2"] > 0.99
        assert model["n_prints"] == 3
        assert model["provenance"]["input_fingerprint"] == job.result_json["input_fingerprint"]
