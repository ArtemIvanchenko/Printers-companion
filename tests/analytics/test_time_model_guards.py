"""Regression counterexamples from the time-prediction architecture audit."""
from copy import deepcopy

import pytest

from analytics.prediction.accuracy import _has_full_layer_coverage
from analytics.prediction.layer_engine import resolve_scan_model
from analytics.prediction.scan_scope import scan_scope, scan_scope_key, snapshot_scan_scope
from analytics.prediction.scan_calibration import _fit, _fit_layer_cycle_model, scan_calibration_report
from analytics.prediction.timing_validation import has_complete_layer_coverage
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from tests.analytics.test_scan_calibration import (
    N_LAYERS, SCAN_PARAMS, THICKNESS, _burn_seconds, _linked_pair, _series,
)
from storage.db.session import SessionLocal


@pytest.mark.parametrize('name', ['_fit', '_fit_beta', '_group_weights', '_gate', '_fit_layer_cycle_model'])
def test_legacy_fit_exports_use_one_detached_numerical_implementation(name):
    from analytics.prediction import scan_calibration, scan_fitting

    assert getattr(scan_calibration, name) is getattr(scan_fitting, name)
    assert getattr(scan_fitting, name).__module__ == 'analytics.prediction.scan_fitting'


def test_detached_fitting_has_no_database_storage_or_http_imports():
    import ast
    import inspect
    from analytics.prediction import scan_fitting

    module = ast.parse(inspect.getsource(scan_fitting))
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            name = node.module or ''
            assert not name.startswith(('storage', 'domain', 'api', 'sqlalchemy', 'core.config'))
        elif isinstance(node, ast.Import):
            assert all(not alias.name.startswith(('storage', 'domain', 'api', 'sqlalchemy')) for alias in node.names)


def _artifact(params=None):
    params = {**SCAN_PARAMS, **(params or {})}
    scope = scan_scope(params, "steel", THICKNESS)
    model = {"scan_calibration_scope": scope, "beta": [0.001, 0, 0, 0, 0, 0]}
    params["scan_model_by_mat"] = {scan_scope_key(scope): model}
    return params, model


@pytest.mark.parametrize("change", [
    {"hatch_speed_mm_s": 2000}, {"contour_speed_mm_s": 900},
    {"support_speed_mm_s": 2000}, {"jump_speed_mm_s": 6000},
    {"jump_delay_ms": 15}, {"hatch_distance_mm": 0.18}, {"laser_count": 1},
    {"printer_id": "other-printer"}, {"printer_id": None},
    {"active_preset_id": "another-preset"}, {"firmware_version": "new"},
    {"process_strategy_version": "new"}, {"slicer_version": "new"},
    {"hatch_speeds_by_mat": {"steel": 2000}},
    {"contours_enabled": False}, {"hatch_angle_deg": 45.0},
])
def test_changed_scan_input_never_reuses_previous_beta(change):
    params, model = _artifact()
    assert resolve_scan_model(params, "steel", THICKNESS) == model
    assert resolve_scan_model({**params, **change}, "steel", THICKNESS) is None


def test_irrelevant_prices_and_result_maps_do_not_change_scope():
    params, model = _artifact()
    altered = {**params, "powder_cost_rub_per_kg": 90000, "time_correction_factor": 1.5,
               "recoat_time_by_mat": {"steel": 9000}, "hatch_speeds_by_mat": {"aluminum": 900}}
    assert resolve_scan_model(altered, "steel", THICKNESS) == model
    # Canonical numeric representation does not depend on 1000 versus 1000.0.
    assert scan_scope({**params, "hatch_speed_mm_s": 1000}, "steel", THICKNESS) == model["scan_calibration_scope"]


def test_legacy_or_forged_artifact_cannot_bypass_scope():
    params, model = _artifact()
    key = next(iter(params["scan_model_by_mat"]))
    params["scan_model_by_mat"] = {"steel@0.100": model, key: {"beta": model["beta"]}}
    assert resolve_scan_model(params, "steel", THICKNESS) is None
    params["scan_model_by_mat"][key] = {**model, "beta": [float("nan")] * 6}
    assert resolve_scan_model(params, "steel", THICKNESS) is None


def test_snapshot_scope_is_not_rebuilt_from_current_defaults():
    params, model = _artifact()
    snapshot = {"material": "steel", "layer_thickness_mm": THICKNESS,
                "hatch_distance_mm": params["hatch_distance_mm"], "laser_count": params["laser_count"],
                "scan_calibration_scope": model["scan_calibration_scope"]}
    assert snapshot_scan_scope(snapshot, params["printer_id"]) == model["scan_calibration_scope"]
    assert snapshot_scan_scope(snapshot, "other-printer") is None
    assert snapshot_scan_scope({**snapshot, "printer_id": "other-printer"}, params["printer_id"]) is None
    assert snapshot_scan_scope({**snapshot, "layer_thickness_mm": 0.06}, params["printer_id"]) is None
    damaged = deepcopy(snapshot)
    damaged["scan_calibration_scope"]["inputs"]["hatch_speed_mm_s"] = 2000
    assert snapshot_scan_scope(damaged, params["printer_id"]) is None
    assert snapshot_scan_scope({}, params["printer_id"]) is None


@pytest.mark.parametrize("expected", [None, 0, -1, 100.0, True, "100"])
def test_unknown_or_invalid_denominator_is_not_full_print(expected):
    rows = dict.fromkeys(range(1, 101), (1.0, 1.0))
    assert not has_complete_layer_coverage(rows, expected)
    assert not _has_full_layer_coverage(rows, expected)


@pytest.mark.parametrize("missing", [1, 50, 100])
def test_even_one_missing_layer_invalidates_full_print_total(missing):
    rows = dict.fromkeys(range(1, 101), (1.0, 1.0))
    assert has_complete_layer_coverage(rows, 100)
    del rows[missing]
    assert not _has_full_layer_coverage(rows, 100)
    rows[101] = (1.0, 1.0)  # same count, wrong physical set
    assert not _has_full_layer_coverage(rows, 100)


@pytest.mark.parametrize("components", [[9500.0] * 101, [9000 + 10.0 * i for i in range(101)]])
def test_floor_only_does_not_invent_eight_second_additive_base(components):
    model = _fit_layer_cycle_model([(components, [18000.0] * len(components))] * 2, ["A", "B"])
    assert model["version"] == "max_base_floor_v2"
    assert model["base_overhead_ms"] is None
    assert model["base_overhead_status"] == "unidentified"


def test_varying_free_branch_preserves_base_with_lower_application_boundary():
    components = [22000.0 + 100 * i for i in range(180)]
    makes = [value + 400.0 for value in components]
    model = _fit_layer_cycle_model([(components, makes)] * 2, ["A", "B"])
    assert model["base_overhead_ms"] == pytest.approx(400.0)
    assert model["base_overhead_status"] == "identified_free_branch"
    assert model["minimum_cycle_ms"] is None
    assert model["minimum_applicable_component_ms"] == 22000.0


def test_opposite_reprint_errors_do_not_cancel_in_geometry_holdout():
    # A repeats twice with opposite errors around B's model. Holding the whole
    # family out must still produce TWO nonzero per-print errors, not zero.
    X = [[float(i), 1.0] for i in range(1, 21)]
    y = [2 * row[0] for row in X]
    model = _fit([(X, [v * 0.8 for v in y]), (X, [v * 1.2 for v in y]), (X, y)], ["A", "A", "B"])
    assert model["cv_error_unit"] == "print_with_geometry_held_out"
    assert len(model["cv_total_errors_pct"]) == 3
    assert model["cv_total_errors_pct"][:2] == pytest.approx([25.0, -16.67])
    assert model["cv_worst_abs_total_err_pct"] == 25.0


def test_different_presets_are_separate_training_pools(tmp_path):
    with SessionLocal() as db:
        for name, speed in (("guard-a", 1000), ("guard-b", 2000)):
            series = _series(1 if speed == 1000 else 0.8)
            burns = {layer: _burn_seconds(series, layer) * 1000 for layer in range(1, N_LAYERS + 1)}
            _linked_pair(db, tmp_path, name, series, burns)
            db.flush()
            row = db.get(PrintRecord, name)
            metadata = deepcopy(row.metadata_json)
            metadata["prediction"]["scan_calibration_scope"] = scan_scope(
                {**SCAN_PARAMS, "hatch_speed_mm_s": speed}, "steel", THICKNESS)
            row.metadata_json = metadata
        db.flush()
        report = scan_calibration_report(db)
        assert len(report["candidates"]) == 2
        assert all(model["n_prints"] == 1 for model in report["candidates"].values())
        assert all(model["status"].startswith("rejected: too_few_prints") for model in report["candidates"].values())
        db.rollback()


def test_old_snapshots_remain_diagnostic_not_new_scope_training(tmp_path):
    with SessionLocal() as db:
        series = _series()
        burns = {layer: _burn_seconds(series, layer) * 1000 for layer in range(1, N_LAYERS + 1)}
        _linked_pair(db, tmp_path, "old-scope", series, burns)
        db.flush()
        row = db.get(PrintRecord, "old-scope")
        metadata = deepcopy(row.metadata_json)
        metadata["prediction"].pop("scan_calibration_scope")
        row.metadata_json = metadata
        db.flush()
        report = scan_calibration_report(db)
        assert report["candidates"] == {}
        assert report["records"][0]["reason"] == "scan_scope_unavailable"
        assert report["records"][0]["cycle_used"] is True
        db.rollback()


@pytest.mark.parametrize("session_printer", ["another-machine", None])
def test_snapshot_cannot_assign_timings_to_an_unknown_or_different_machine(tmp_path, session_printer):
    with SessionLocal() as db:
        series = _series()
        burns = {layer: _burn_seconds(series, layer) * 1000 for layer in range(1, N_LAYERS + 1)}
        _linked_pair(db, tmp_path, "wrong-machine", series, burns)
        db.flush()
        db.get(BuildSession, "s_wrong-machine").printer_id = session_printer
        db.flush()
        report = scan_calibration_report(db)
        assert report["candidates"] == {}
        assert report["cycle_candidates"] == {}
        assert report["records"][0]["cycle_used"] is False
        db.rollback()
