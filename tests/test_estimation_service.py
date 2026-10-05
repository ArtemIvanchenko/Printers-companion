"""Regression coverage for the estimate application boundary and publication."""

from datetime import datetime, timezone

import pytest

from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository


def test_print_scan_strategy_overrides_global_speed_and_keeps_source():
    from unittest.mock import MagicMock
    from domain.services.estimation.inputs import _params_with_sources_for_record

    repo = MagicMock()
    original_speeds = {"steel": 1000.0, "aluminum": 950.0}
    repo.get_machine_params.return_value = {"hatch_speed_mm_s": 1000.0,
                                            "hatch_speeds_by_mat": original_speeds}
    repo.get_active_preset_for_material.return_value = {"preset_id": "old-preset",
                                                      "hatch_speed_mm_s": 1100.0}
    record = {"material": "steel", "metadata_json": {"scan_strategy": {
        "hatch_speed_mm_s": 1330.0, "support_speed_mm_s": 2000.0,
        "contours_enabled": False, "hatch_angle_deg": 45.0,
    }}}
    params, sources = _params_with_sources_for_record(repo, record)
    assert params["hatch_speed_mm_s"] == 1330.0
    assert "steel" not in params["hatch_speeds_by_mat"]
    assert original_speeds == {"steel": 1000.0, "aluminum": 950.0}
    assert params["contours_enabled"] is False
    assert sources["hatch_angle_deg"] == {"source": "print_scan_strategy", "value": 45.0}


def test_disabled_contours_do_not_require_an_unused_speed():
    from domain.services.estimation.inputs import missing_for_estimation

    params = {"hatch_speed_mm_s": 1330.0, "hatch_distance_mm": 0.095,
              "layer_thickness_mm": 0.03, "contours_enabled": False}
    assert missing_for_estimation(params) == []
    assert "скорость контуров" in missing_for_estimation({**params, "contours_enabled": True})


@pytest.mark.parametrize('status', ['pending', 'running'])
def test_manual_click_joins_same_active_automatic_estimate(queued_estimate, status):
    from domain.services.estimation.requests import enqueue_estimate

    record, _ = queued_estimate
    with session_scope() as db:
        repo = PrintsRepository(db)
        automatic = enqueue_estimate(repo, record)
        row = db.get(BackgroundJob, automatic['job_id'])
        row.status = status
        row.lease_generation = 7
        row.lease_owner = 'original-worker'
        db.flush()
        manual = enqueue_estimate(repo, record, force=True)
        assert manual['job_id'] == automatic['job_id']
        assert row.lease_generation == 7 and row.lease_owner == 'original-worker'


def test_manual_rerun_after_completion_is_a_new_job(queued_estimate):
    from domain.services.estimation.requests import enqueue_estimate

    record, _ = queued_estimate
    with session_scope() as db:
        repo = PrintsRepository(db)
        first = enqueue_estimate(repo, record)
        db.get(BackgroundJob, first['job_id']).status = 'done'
        db.flush()
        assert enqueue_estimate(repo, record, force=True)['job_id'] != first['job_id']


def test_changed_parameters_do_not_join_old_active_estimate(queued_estimate):
    from domain.services.estimation.requests import enqueue_estimate

    record, _ = queued_estimate
    with session_scope() as db:
        repo = PrintsRepository(db)
        first = enqueue_estimate(repo, record)
        repo.save_machine_params({'hatch_speed_mm_s': 1800})
        new = enqueue_estimate(repo, record, force=True)
        assert new['job_id'] != first['job_id']
        assert new['payload']['input_fingerprint'] != first['payload']['input_fingerprint']


@pytest.mark.parametrize("strategy", [False, "preset", {"contours_enabled": "false"},
                                      {"hatch_speed_mm_s": 0}, {"laser_count": 1.5},
                                      {"hatch_speed_mm_s": 10 ** 400}])
def test_invalid_print_strategy_is_not_treated_as_defaults(strategy):
    from unittest.mock import MagicMock
    from domain.services.estimation.contracts import EstimateError
    from domain.services.estimation.inputs import params_for_record

    repo = MagicMock()
    repo.get_machine_params.return_value = {}
    repo.get_active_preset_for_material.return_value = None
    with pytest.raises(EstimateError):
        params_for_record(repo, {"material": "steel", "metadata_json": {"scan_strategy": strategy}})


@pytest.fixture
def queued_estimate():
    owner = get_settings().compute_node_id
    with session_scope() as db:
        repo = PrintsRepository(db)
        repo.save_machine_params(
            {
                "hatch_speed_mm_s": 1000,
                "contour_speed_mm_s": 500,
                "hatch_distance_mm": 0.1,
                "layer_thickness_mm": 0.06,
                "laser_count": 1,
            }
        )
        record = repo.create_print_record(
            {
                "name": "publication fixture",
                "material": "steel",
                "origin_compute_node_id": owner,
                "metadata_json": {"keep": "operator context"},
            }
        )
        repo.add_print_file(
            {
                "record_id": record["record_id"],
                "file_name": "body.stl",
                "file_type": "stl",
                "checksum": "a" * 64,
                "size_bytes": 1,
                "object_uri": "s3://stls/body.stl",
            }
        )
        record = repo.get_print_record(record["record_id"])
        job = JobsRepository(db).enqueue(
            job_type="print_estimate",
            owner_node_id=owner,
            entity_type="print_record",
            entity_id=record["record_id"],
            idempotency_key="estimation-boundary",
            payload={
                "record_id": record["record_id"],
                "record_revision": record["revision"],
                "owner_node_id": owner,
            },
        )
    return record, job


def _snapshot(prepared):
    from domain.services.estimation.inputs import prediction_input_hash as _prediction_input_hash

    return {
        "input_hash": _prediction_input_hash(prepared),
        "input_revision": prepared["record"]["revision"],
        "print_hours": 1.0,
        "correction_factor": 1.0,
        "prediction_source": "model",
        "estimated_at": datetime.now(timezone.utc).isoformat(),
    }


def test_machine_settings_changed_during_estimate_cannot_publish(queued_estimate, monkeypatch):
    from worker.estimate_tasks import process_next_estimate

    def calculate(prepared, **kwargs):
        with session_scope() as db:
            PrintsRepository(db).save_machine_params({"hatch_speed_mm_s": 2000})
        return _snapshot(prepared)

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", calculate
    )
    record, job = queued_estimate
    assert process_next_estimate("estimation-test-worker")
    with session_scope() as db:
        assert db.get(BackgroundJob, job["job_id"]).status == "failed"
        assert not (
            PrintsRepository(db).get_print_record(record["record_id"])["metadata_json"] or {}
        ).get("prediction")


def test_expiry_while_writing_rolls_back_card_and_completion(queued_estimate, monkeypatch):
    from worker.estimate_tasks import process_next_estimate

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot",
        lambda prepared, **kwargs: _snapshot(prepared),
    )
    record, job = queued_estimate
    original = PrintsRepository.update_print_record

    def expire(repo, record_id, values, **kwargs):
        row = repo.db.get(BackgroundJob, job["job_id"])
        row.lease_until = datetime(2000, 1, 1, tzinfo=timezone.utc)
        repo.db.flush()
        return original(repo, record_id, values, **kwargs)

    monkeypatch.setattr(PrintsRepository, "update_print_record", expire)
    assert process_next_estimate("estimation-test-worker")
    with session_scope() as db:
        assert db.get(BackgroundJob, job["job_id"]).status != "done"
        assert not (
            PrintsRepository(db).get_print_record(record["record_id"])["metadata_json"] or {}
        ).get("prediction")


def _assert_no_prediction(record, job, status="failed"):
    with session_scope() as db:
        assert db.get(BackgroundJob, job["job_id"]).status == status
        current = PrintsRepository(db).get_print_record(record["record_id"])
        assert current["metadata_json"].get("prediction") is None
        assert current["metadata_json"]["keep"] == "operator context"


def test_successful_worker_preserves_context_and_saves_the_same_result(
    queued_estimate, monkeypatch
):
    from worker.estimate_tasks import process_next_estimate

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot",
        lambda prepared, **kwargs: _snapshot(prepared),
    )
    record, job = queued_estimate
    assert process_next_estimate("estimation-test-worker")
    with session_scope() as db:
        saved = db.get(BackgroundJob, job["job_id"])
        assert saved.status == "done" and saved.lease_owner is None
        current = PrintsRepository(db).get_print_record(record["record_id"])
        assert current["metadata_json"]["keep"] == "operator context"
        assert current["metadata_json"]["prediction"] == saved.result_json["prediction"]
        assert current["metadata_json"]["prediction"]["input_revision"] == record["revision"]


@pytest.mark.parametrize("action", ["insert", "edit", "delete", "switch"])
def test_preset_changes_invalidate_running_estimate(queued_estimate, monkeypatch, action):
    from worker.estimate_tasks import process_next_estimate

    with session_scope() as db:
        repo = PrintsRepository(db)
        if action != "insert":
            first = repo.create_preset(
                {"name": "first", "material": "steel", "is_default": True, "hatch_speed_mm_s": 1500}
            )
        if action == "switch":
            second = repo.create_preset(
                {
                    "name": "second",
                    "material": "steel",
                    "is_default": False,
                    "hatch_speed_mm_s": 1700,
                }
            )

    def calculate(prepared, **kwargs):
        with session_scope() as db:
            repo = PrintsRepository(db)
            if action == "insert":
                repo.create_preset(
                    {
                        "name": "inserted",
                        "material": "steel",
                        "is_default": True,
                        "hatch_speed_mm_s": 2000,
                    }
                )
            elif action == "edit":
                repo.update_preset(first["preset_id"], {"hatch_speed_mm_s": 2000})
            elif action == "delete":
                repo.delete_preset(first["preset_id"])
            else:
                repo.set_default_preset(second["preset_id"])
        return _snapshot(prepared)

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", calculate
    )
    assert process_next_estimate("estimation-test-worker")
    _assert_no_prediction(*queued_estimate)


@pytest.mark.parametrize(
    "change", ["card", "checksum", "uri", "snapshot_hash", "snapshot_revision"]
)
def test_changed_inputs_or_mixed_snapshot_are_rejected(queued_estimate, monkeypatch, change):
    from domain.models.prints import PrintRecordFile
    from sqlalchemy import select
    from worker.estimate_tasks import process_next_estimate

    record, job = queued_estimate

    def calculate(prepared, **kwargs):
        snapshot = _snapshot(prepared)
        with session_scope() as db:
            if change == "card":
                PrintsRepository(db).update_print_record(record["record_id"], {"notes": "edited"})
            elif change in {"checksum", "uri"}:
                row = db.scalar(
                    select(PrintRecordFile).where(PrintRecordFile.record_id == record["record_id"])
                )
                if change == "checksum":
                    row.checksum = "b" * 64
                else:
                    row.object_uri = "s3://stls/new-body.stl"
        if change == "snapshot_hash":
            snapshot["input_hash"] = "different-inputs"
        elif change == "snapshot_revision":
            snapshot["input_revision"] += 1
        return snapshot

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", calculate
    )
    assert process_next_estimate("estimation-test-worker")
    _assert_no_prediction(record, job)


def test_lost_generation_does_not_change_replacement_worker_state(queued_estimate, monkeypatch):
    from worker.estimate_tasks import process_next_estimate

    record, job = queued_estimate

    def calculate(prepared, **kwargs):
        with session_scope() as db:
            row = db.get(BackgroundJob, job["job_id"])
            row.lease_generation += 1
            row.lease_owner = "replacement-worker"
        return _snapshot(prepared)

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", calculate
    )
    assert process_next_estimate("estimation-test-worker")
    _assert_no_prediction(record, job, "running")
    with session_scope() as db:
        assert db.get(BackgroundJob, job["job_id"]).lease_owner == "replacement-worker"


def test_failed_card_write_cannot_leave_a_completed_job(queued_estimate, monkeypatch):
    from worker.estimate_tasks import process_next_estimate

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot",
        lambda prepared, **kwargs: _snapshot(prepared),
    )
    original = PrintsRepository.update_print_record

    def write_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("write failure")

    monkeypatch.setattr(PrintsRepository, "update_print_record", write_then_fail)
    assert process_next_estimate("estimation-test-worker")
    _assert_no_prediction(*queued_estimate, status="pending")


def test_calculation_and_interval_hold_no_sql_connection(queued_estimate, monkeypatch):
    from sqlalchemy import event
    from storage.db.session import engine
    from worker.estimate_tasks import process_next_estimate

    held = set()
    calls = []

    def checkout(connection, record, proxy):
        held.add(id(connection))

    def checkin(connection, record):
        held.discard(id(connection))

    def calculate(prepared, **kwargs):
        assert not held
        calls.append("calculation")
        return _snapshot(prepared)

    def interval(snapshot, *, inputs):
        assert not held and inputs is not None
        calls.append("interval")

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", calculate
    )
    monkeypatch.setattr(
        "domain.services.estimation.calculation.needs_prediction_interval", lambda snapshot: True
    )
    monkeypatch.setattr(
        "domain.services.estimation.calculation.enrich_prediction_interval", interval
    )
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        assert process_next_estimate("estimation-test-worker")
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert calls == ["calculation", "interval"]


def _interval_snapshot(**overrides):
    return {
        "material": "steel",
        "layer_thickness_mm": 0.06,
        "build_origin_source": "explicit",
        "build_origin_z_mm": 0.0,
        "estimate_quality": "standard",
        "prediction_source": "calculated",
        "print_hours": 2.0,
        "machine_cycle_hours": 2.0,
        "raw_scan_hours": 1.5,
        "raw_recoat_hours": 0.5,
        "layer_overhead_ms": None,
        "prediction_warnings": ["operator warning"],
        **overrides,
    }


def test_prediction_interval_uses_only_detached_history(monkeypatch):
    from analytics.prediction.calibration_inputs import CalibrationInputs
    from domain.services.estimation.calculation import enrich_prediction_interval

    history = CalibrationInputs(linked=[], timing_rows=[], params=None)
    snapshot = _interval_snapshot()
    calls = []

    def interval(db, material, thickness, scan, recoat, *, inputs):
        assert db is None and inputs is history
        assert (material, thickness, scan, recoat) == ("steel", 0.06, 1.5, 0.5)
        calls.append("detached")
        return (3.0, 4.0)

    monkeypatch.setattr("analytics.prediction.accuracy.calibration_interval_hours", interval)
    enrich_prediction_interval(snapshot, inputs=history)
    assert snapshot["prediction_interval"] == [3.0, 4.0]
    assert snapshot["interval_reference"]["input_fingerprint"] == history.input_fingerprint
    assert snapshot["interval_reference"]["scope"] == "scan_plus_recoat"
    assert datetime.fromisoformat(snapshot["interval_reference"]["computed_at"]).tzinfo is not None
    assert snapshot["print_hours"] == 2.0 and snapshot["machine_cycle_hours"] == 2.0
    assert snapshot["prediction_warnings"][0] == "operator warning"
    assert len(snapshot["prediction_warnings"]) == 2
    enrich_prediction_interval(snapshot, inputs=history)
    assert calls == ["detached", "detached"]
    assert len(snapshot["prediction_warnings"]) == 2


@pytest.mark.parametrize("overrides", [
    {"prediction_source": "model"},
    {"layer_overhead_ms": 0.0},
    {"layer_overhead_ms": 500.0, "minimum_layer_cycle_ms": 15_000.0},
    {"estimate_quality": "lower_bound"},
    {"build_origin_source": "minimum_supplied_geometry_z"},
])
def test_ineligible_prediction_never_reads_interval_history(monkeypatch, overrides):
    from analytics.prediction.calibration_inputs import CalibrationInputs
    from domain.services.estimation.calculation import enrich_prediction_interval

    calls = []

    def unexpected(*args, **kwargs):
        calls.append("interval")
        raise AssertionError("ineligible prediction requested an interval")

    monkeypatch.setattr("analytics.prediction.accuracy.calibration_interval_hours", unexpected)
    snapshot = _interval_snapshot(**overrides)
    previous = dict(snapshot)
    enrich_prediction_interval(
        snapshot, inputs=CalibrationInputs(linked=[], timing_rows=[], params=None),
    )
    assert snapshot == previous
    assert not calls


def test_missing_interval_history_does_not_fabricate_uncertainty():
    from analytics.prediction.calibration_inputs import CalibrationInputs
    from domain.services.estimation.calculation import enrich_prediction_interval

    snapshot = _interval_snapshot()
    previous = dict(snapshot)
    enrich_prediction_interval(
        snapshot, inputs=CalibrationInputs(linked=[], timing_rows=[], params=None),
    )
    assert snapshot == previous


def test_prediction_calculation_cannot_accept_a_sql_session():
    import inspect
    from domain.services.estimation.calculation import (
        calculate_prediction_snapshot, combined_prediction, enrich_prediction_interval,
    )

    for function in (calculate_prediction_snapshot, combined_prediction, enrich_prediction_interval):
        assert "db" not in inspect.signature(function).parameters
    parameter = inspect.signature(enrich_prediction_interval).parameters["inputs"]
    assert parameter.default is inspect.Parameter.empty


def test_fresh_worker_process_does_not_import_web_routes():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class NoWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'api', 'fastapi', 'starlette'}:
            raise AssertionError('worker tried to import HTTP: ' + fullname)
sys.meta_path.insert(0, NoWeb())
from worker import estimate_tasks, nas_sync
from domain.services.estimation import inputs, calculation, publication, requests
from domain.services.print_cards import cards, attachments, validation
from domain.services.estimation.contracts import EstimateError
from storage.db.session import session_scope
from storage.repositories.prints_repo import PrintsRepository
with session_scope() as db:
    record = PrintsRepository(db).create_print_record({'name': 'no web imports', 'material': 'steel'})
    try:
        inputs.prepare_prediction_inputs(PrintsRepository(db), record['record_id'])
    except EstimateError as exc:
        assert exc.code == 'invalid_inputs' and 'STL' in exc.detail
    else:
        raise AssertionError('missing STL should be rejected')
print('worker boundary OK')
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "worker boundary OK" in result.stdout


def test_domain_error_survives_serialization():
    import pickle
    from domain.services.estimation.contracts import EstimateError

    error = pickle.loads(pickle.dumps(EstimateError("invalid_inputs", {"message": "нет STL"})))
    assert error.code == "invalid_inputs" and error.detail == {"message": "нет STL"}


def test_request_id_middleware_compatibility_and_headers():
    from api.main import app
    from api.middleware import RequestIDMiddleware
    from core.logging.config import RequestIDMiddleware as LegacyMiddleware
    from fastapi.testclient import TestClient

    assert LegacyMiddleware is RequestIDMiddleware
    client = TestClient(app)
    response = client.get("/health", headers={"X-Request-ID": "refactor-check"})
    assert response.headers["X-Request-ID"] == "refactor-check"
    assert client.get("/health").headers["X-Request-ID"] != "refactor-check"
