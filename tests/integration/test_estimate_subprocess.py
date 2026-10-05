"""The production queue consumer works across a fresh process boundary.

HTTP never performs geometry work, even under APP_ENV=test. A bare card lets
the real worker exercise claim/rejection/fenced failure without MinIO or STL.
"""

import ast
import inspect
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository
from worker.estimate_tasks import process_next_estimate


def test_worker_claims_and_records_rejection_in_a_fresh_process():
    owner = get_settings().compute_node_id
    with session_scope() as db:
        row = PrintRecord(record_id="pr_subprocess", name="без STL", origin_compute_node_id=owner)
        db.add(row)
        db.flush()
        job = JobsRepository(db).enqueue(
            job_type="print_estimate", owner_node_id=owner,
            entity_type="print_record", entity_id=row.record_id,
            idempotency_key="subprocess-estimate", payload={
                "record_id": row.record_id, "record_revision": row.revision,
                "owner_node_id": owner,
            },
        )

    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        assert pool.submit(process_next_estimate, "subprocess-test-worker", owner).result(timeout=60)

    with session_scope() as db:
        saved = db.get(BackgroundJob, job["job_id"])
        assert saved.status == "failed"
        assert "не прикреплён STL" in saved.error
        assert saved.lease_generation == 1
        assert not (db.get(PrintRecord, "pr_subprocess").metadata_json or {}).get("prediction")


def test_http_rejects_missing_inputs_without_running_a_calculator(monkeypatch):
    def unexpected_calculation(*args, **kwargs):
        raise AssertionError("HTTP must not execute the estimate")

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", unexpected_calculation,
    )
    client = TestClient(app)
    record = client.post("/prints", json={"name": "без STL"}).json()
    response = client.post(f"/prints/{record['record_id']}/estimate")
    assert response.status_code == 422
    assert "не прикреплён STL" in response.json()["detail"]


@pytest.mark.parametrize("environment", ["test", "production"])
def test_http_only_queues_valid_estimates_in_every_environment(monkeypatch, environment):
    owner = get_settings().compute_node_id
    with session_scope() as db:
        repo = PrintsRepository(db)
        repo.save_machine_params({
            "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": 0.1,
            "layer_thickness_mm": 0.06, "laser_count": 1,
        })
        record = repo.create_print_record({"name": "queued", "origin_compute_node_id": owner})
        repo.add_print_file({
            "record_id": record["record_id"], "file_name": "body.stl", "file_type": "stl",
            "checksum": "a" * 64, "size_bytes": 1, "object_uri": "s3://stls/body.stl",
        })

    calls = []
    def unexpected_calculation(*args, **kwargs):
        calls.append("calculated in HTTP")
        raise AssertionError("HTTP must not execute the estimate")

    monkeypatch.setattr(
        "domain.services.estimation.calculation.calculate_prediction_snapshot", unexpected_calculation,
    )
    settings = get_settings().model_copy(update={"app_env": environment})
    monkeypatch.setattr("api.routes.prints.get_settings", lambda: settings)
    response = TestClient(app).post(f"/prints/{record['record_id']}/estimate")

    assert response.status_code == 200, response.text
    assert not calls
    with session_scope() as db:
        saved = db.get(BackgroundJob, response.json()["job_id"])
        assert saved.status == "pending" and saved.lease_generation == 0
        assert saved.owner_node_id == owner
        assert not (db.get(PrintRecord, record["record_id"]).metadata_json or {}).get("prediction")


def test_print_routes_have_no_test_only_execution_path_or_process_pool():
    from api.routes import prints

    tree = ast.parse(inspect.getsource(prints))
    assert not any(isinstance(node, ast.Attribute) and node.attr == "app_env" for node in ast.walk(tree))
    assert not hasattr(prints, "_auto_estimate")
    assert not hasattr(prints, "_compute_prediction_snapshot")
    assert not hasattr(prints, "_ESTIMATE_POOL")
