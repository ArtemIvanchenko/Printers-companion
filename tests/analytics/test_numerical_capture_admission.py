"""Historical quotes survive incompatible numerical captures without training them.

Native STL calculations and synthetic integer-ms observations exercise the usual
HTTP/owner-local consumer. They do not prove a physical recipe or held-out fit.
"""
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import trimesh
from fastapi.testclient import TestClient

from analytics.prediction import scan_calibration
from analytics.prediction.accuracy import calibration_interval_hours, prediction_accuracy
from analytics.prediction.calibration import latest_calibration_report
from analytics.prediction.calibration_inputs import load_calibration_inputs
from analytics.prediction.recoat_calibration import recoat_accuracy
from analytics.prediction.timing_snapshot import read_timing_publication
from core.config.settings import get_settings
from core.versioning.constants import ANALYSIS_VERSION
from core.versioning.provenance import stable_hash
from domain.models.events import LayerSnapshot
from domain.models.prints import MachineParams, PrintRecord, PrintRecordFile
from domain.models.sessions import BuildSession, Printer, PrinterProfile
from domain.services.estimation.calculation import calculate_prediction_snapshot
from domain.services.estimation.inputs import prediction_configuration, prepare_prediction_inputs
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository
from worker.calibration_tasks import process_next_calibration_task

MODE = "steel@0.050"
REASON = "incompatible_prediction_generation"
OLD_VERSION = "0.5.8"


@pytest.fixture
def history_factory():
    node = get_settings().compute_node_id
    blobs = {}
    for name, width, x in (("part.stl", 5., 0.), ("support.stl", .2, 7.)):
        mesh = trimesh.creation.box(extents=[width, 5., 5.])
        mesh.apply_translation([x, 0., -float(mesh.bounds[0][2])])
        blobs[name] = mesh.export(file_type="stl")

    class MemoryStore:
        def download_file(self, bucket, name, destination, *, expected_sha256=None):
            assert bucket == "isolated"
            assert hashlib.sha256(blobs[name]).hexdigest() == expected_sha256
            Path(destination).write_bytes(blobs[name])
            return Path(destination)

    def create(generations):
        with session_scope() as db:
            db.add(PrinterProfile(profile_id="capture-profile", vendor="synthetic",
                                  model_family="synthetic", current_version="1"))
        with session_scope() as db:
            db.add(Printer(printer_id="capture-printer", name="Synthetic coupon fixture",
                           vendor="synthetic", model_family="synthetic", profile_id="capture-profile"))
            db.add(MachineParams(id=1, hatch_speed_mm_s=1550., contour_speed_mm_s=500.,
                hatch_distance_mm=.12, layer_thickness_mm=.05, laser_count=1,
                recoat_time_ms=10000., jump_speed_mm_s=5000., jump_delay_ms=0.,
                time_correction_factor=1., correction_locked=False, material_densities={"steel": 7.9}))
        for index, generation in enumerate(generations):
            rid, sid = f"capture-record-{index}", f"capture-session-{index}"
            when = datetime(2026, 10, index + 1, tzinfo=timezone.utc)
            timed = [{"layer": layer, "features": {
                "burn_ms": 210, "pour_ms": 10000, "make_layer_ms": 10710}}
                for layer in range(1, 101)]
            manifest = {"schema": 1, "status": "complete", "row_count": 100,
                        "rows_fingerprint": stable_hash(timed)}
            manifest["publication_id"] = stable_hash(manifest)
            assert read_timing_publication([
                (row["layer"], row["features"], manifest["publication_id"]) for row in timed
            ], manifest)[0] == "complete"
            with session_scope() as db:
                db.add(BuildSession(session_id=sid, printer_id="capture-printer",
                    origin_compute_node_id=node, start_ts=when, end_ts=when + timedelta(seconds=1071),
                    classification="REAL_PRINT", context={"timing_publication": manifest}))
                db.add(PrintRecord(record_id=rid, name="Synthetic precision coupon", material="steel",
                    session_id=sid, origin_compute_node_id=node, layer_thickness_mm=.05,
                    hatch_distance_mm=.12, powder_cost_rub_per_kg=45000., created_at=when,
                    metadata_json={"geometry_quality": {"status": "complete"},
                        "build_origin_z_mm": 0., "session_link_confirmed": True,
                        "scan_strategy": {"support_speed_mm_s": 2000., "contours_enabled": False,
                                          "hatch_angle_deg": 67.}}))
                db.flush()
                db.add_all([PrintRecordFile(record_id=rid, object_uri="s3://isolated/" + name,
                    file_name=name, file_type="stl" if name == "part.stl" else "stl_supports",
                    size_bytes=len(data), checksum=hashlib.sha256(data).hexdigest(),
                    uploaded_at=when + timedelta(seconds=offset))
                    for offset, (name, data) in enumerate(blobs.items())])
                db.add_all([LayerSnapshot(session_id=sid, layer=row["layer"], features=row["features"],
                    context={"publication_id": manifest["publication_id"]}) for row in timed])
            with session_scope() as db:
                prepared = prepare_prediction_inputs(PrintsRepository(db), rid, compute_node_id=node)
            snapshot = calculate_prediction_snapshot(prepared, object_store_factory=MemoryStore,
                                                     computed_by=node)
            if generation == "old":
                # Reproduce the former policy on synthetic inputs; never relabel
                # a real historical record or pretend rounded values are exact.
                for field in ("print_hours", "raw_print_hours", "raw_scan_hours", "raw_recoat_hours",
                              "scan_hours", "recoat_hours"):
                    snapshot[field] = round(snapshot[field], 3)
                snapshot["analysis_version"] = OLD_VERSION
                snapshot["provenance"]["analysis_version"] = OLD_VERSION
                configuration = prediction_configuration(prepared)
                configuration["analysis_version"] = OLD_VERSION
                snapshot["provenance"]["config_hash"] = stable_hash(configuration)
            elif generation == "missing":
                snapshot.pop("analysis_version")
            elif generation == "missing_provenance":
                snapshot.pop("provenance")
            elif generation == "unknown":
                snapshot["analysis_version"] = snapshot["provenance"]["analysis_version"] = "unknown"
            elif generation == "conflicting_top":
                snapshot["analysis_version"] = OLD_VERSION
            elif generation == "conflicting_provenance":
                snapshot["provenance"]["analysis_version"] = OLD_VERSION
            elif generation == "foreign_component":
                snapshot["provenance"]["component"] = "other-estimator"
            elif generation == "malformed_provenance":
                snapshot["provenance"] = [ANALYSIS_VERSION]
            elif generation == "nonstring_version":
                snapshot["analysis_version"] = snapshot["provenance"]["analysis_version"] = True
            else:
                assert generation == "current"
            with session_scope() as db:
                record = db.get(PrintRecord, rid)
                assert record.revision == snapshot["input_revision"] == 1
                record.metadata_json = {**record.metadata_json, "prediction": snapshot}
        return _inputs()

    return create


def _inputs():
    with session_scope() as db:
        return load_calibration_inputs(db)


def _run_calibration(client):
    accepted = client.post("/prints/recalibrate")
    assert accepted.status_code == 202
    assert process_next_calibration_task("capture-test-worker", get_settings().compute_node_id)
    with session_scope() as db:
        job = JobsRepository(db).get(accepted.json()["job_id"])
        assert job["status"] == "done", job
        assert job["result"]["status"] == "published"
        params = db.get(MachineParams, 1)
        assert latest_calibration_report(db) == job["result"]
        return job["result"], deepcopy(params.time_correction_by_mat or {})


@pytest.mark.parametrize("generation", [
    "old", "missing", "missing_provenance", "unknown", "conflicting_top",
    "conflicting_provenance", "foreign_component", "malformed_provenance", "nonstring_version",
])
def test_incompatible_capture_preserves_history_recoat_and_log_only_cycle(history_factory, monkeypatch, generation):
    inputs = history_factory([generation] * 3)
    before = deepcopy(inputs)
    report = prediction_accuracy(inputs=inputs)
    assert report["n_pairs"] == 3 and report["n_usable_pairs"] == 0
    assert report["by_mode"][MODE]["suggested_factor"] is None
    assert report["by_mode"][MODE]["ratio_interval"] is None
    assert all(row["excluded_reason"] == REASON and row["excluded_reason_ru"] for row in report["pairs"])
    assert all(row["actual_source"] == "machine_log" and row["error_pct"] is not None for row in report["pairs"])
    assert calibration_interval_hours(None, "steel", .05, .1, 1., inputs=inputs) is None

    def forbidden(*args, **kwargs):
        pytest.fail("Incompatible geometry must not reach NNLS preparation or fitting")

    monkeypatch.setattr(scan_calibration, "_pairs_from_record", forbidden)
    monkeypatch.setattr(scan_calibration, "_fit", forbidden)
    scan = scan_calibration.scan_calibration_report(inputs=inputs)
    assert scan["candidates"] == {} and scan["cycle_candidates"]
    assert all(row["used"] is False and row["reason"] == REASON and row["cycle_used"] is True
               for row in scan["records"])
    recoat = recoat_accuracy(inputs=inputs)
    assert recoat["n_usable_sessions"] == 3
    assert recoat["by_material"]["steel"]["suggested_recoat_ms"] == 10000.
    assert inputs == before


def test_old_rounded_capture_does_not_publish_a_new_ratio_via_normal_consumer(history_factory):
    initial = history_factory(["old"] * 3)
    from api.main import app

    client = TestClient(app)  # no lifespan/startup/background runtime
    try:
        result, factors = _run_calibration(client)
        assert result["provenance"]["analysis_version"] == ANALYSIS_VERSION
        assert factors == {} and result["applied"] == {}
        assert result["recoat"]["applied"] == {"steel": 10000.}
        view = client.get("/prints/prediction-accuracy").json()
        assert view["n_pairs"] == 3 and view["n_usable_pairs"] == 0
        assert view["by_mode"][MODE]["suggested_factor"] is None
        assert all(row["scan_ratio"] == .972 and row["excluded_reason"] == REASON for row in view["pairs"])
        assert all(row["cycle_used"] and row["reason"] == REASON for row in result["scan_report"]["records"])
        after = _inputs()
        assert after.input_fingerprint == initial.input_fingerprint
        assert after.linked == initial.linked and after.timing_rows == initial.timing_rows
    finally:
        client.close()


def test_current_native_capture_remains_usable_after_self_apply_twice(history_factory):
    initial = history_factory(["current"] * 3)
    raw_scan = initial.linked[0][0].metadata_json["prediction"]["raw_scan_hours"]
    assert raw_scan * 3600 == pytest.approx(20.972625560261626, rel=1e-14)
    assert round(21. / (raw_scan * 3600), 3) == 1.001
    from api.main import app

    client = TestClient(app)  # no lifespan/startup/background runtime
    try:
        assert client.get("/prints/capture-record-0").json()["prediction_configuration_is_current"] is True
        for iteration in range(2):
            result, factors = _run_calibration(client)
            assert factors == {MODE: 1.001}
            assert result["applied"] == ({MODE: 1.001} if iteration == 0 else {})
            after = _inputs()
            assert after.input_fingerprint == initial.input_fingerprint
            assert after.linked == initial.linked and after.timing_rows == initial.timing_rows
            view = client.get("/prints/prediction-accuracy").json()
            assert view["n_pairs"] == view["n_usable_pairs"] == 3
            assert view["by_mode"][MODE]["suggested_factor"] == 1.001
            assert view["scan"]["status"] == "current"
            for index in range(3):
                card = client.get(f"/prints/capture-record-{index}").json()
                assert card["prediction_is_current"] is True
                assert card["prediction_configuration_is_current"] is False
        assert after.config_fingerprint != initial.config_fingerprint
    finally:
        client.close()


def test_mixed_history_only_current_capture_trains_the_new_factor(history_factory):
    inputs = history_factory(["current"] * 3 + ["old"] * 3)
    before = deepcopy(inputs)
    report = prediction_accuracy(inputs=inputs)
    assert report["n_pairs"] == 6 and report["n_usable_pairs"] == 3
    assert report["by_mode"][MODE]["suggested_factor"] == 1.001
    assert report["by_mode"][MODE]["ratio_interval"] == (1.001, 1.001)
    historical = [row for row in report["pairs"] if row["excluded_reason"] == REASON]
    assert len(historical) == 3 and all(row["scan_ratio"] == .972 for row in historical)
    scan = scan_calibration.scan_calibration_report(inputs=inputs)
    assert sum(row["used"] for row in scan["records"]) == 3
    assert all(row["cycle_used"] for row in scan["records"])
    assert inputs == before
