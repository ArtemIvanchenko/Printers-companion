"""Regression coverage for the estimate application boundary and publication."""

from datetime import datetime, timezone

import pytest

from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository


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

    def interval(snapshot, db=None, *, inputs=None):
        assert not held and db is None and inputs is not None
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
