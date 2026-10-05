from copy import deepcopy
from itertools import permutations
import sys

import pytest

from analytics.prediction.recoat_calibration import layer_seconds_from_events
from analytics.prediction.scan_calibration import _burn_seconds_by_layer, _layer_cycles_ms_by_layer
from analytics.prediction.timing_evidence import summarize_timing_events
from analytics.prediction.timing_validation import calibration_timing_payloads
from analytics.prediction.timing_snapshot import read_timing_publication
from core.versioning.provenance import stable_hash


def event(layer=1, burn=30_000, pour=9_000, make=39_300, **extra):
    return {"event_type": "layer_timing_summary", "payload": {
        "layer": layer, "burn_ms": burn, "pour_ms": pour, "make_layer_ms": make, **extra,
    }}


@pytest.mark.parametrize("bad", [
    event(timing_valid=False), event(make=500), event(burn=float("nan")),
    event(burn=float("inf")), event(make=float("inf")), event(burn=True),
    event(layer=0), event(layer=True),
])
def test_invalid_evidence_never_enters_calibration_or_factual_totals(bad):
    for extract in (calibration_timing_payloads, layer_seconds_from_events,
                    _burn_seconds_by_layer, _layer_cycles_ms_by_layer):
        assert extract([bad]) == {}
    assert summarize_timing_events([bad]).invalid_rows == 1


@pytest.mark.parametrize("retry", [
    event(pour=10_000, make=40_300),
    event(make=50_000),  # same burn/pour does not prove same physical attempt
    event(pour=500_000, make=530_300),
    event(timing_valid=False),
])
def test_conflicts_are_excluded_regardless_of_file_order(retry):
    for rows in permutations([event(), retry, event(layer=2)]):
        for extract in (calibration_timing_payloads, layer_seconds_from_events,
                        _burn_seconds_by_layer, _layer_cycles_ms_by_layer):
            assert set(extract(rows)) == {2}


def test_equivalent_boundary_copies_are_counted_once():
    rows = [event(), event(burn=30_010, make=39_310)]
    assert layer_seconds_from_events(rows) == {1: (30.0, 9.0)}


def _count_timing_admissions(action):
    code = calibration_timing_payloads.__code__
    calls = 0
    previous = sys.getprofile()

    def observe(frame, kind, arg):
        nonlocal calls
        if kind == "call" and frame.f_code is code:
            calls += 1

    sys.setprofile(observe)
    try:
        result = action()
    finally:
        sys.setprofile(previous)
    return result, calls


def test_card_comparison_uses_one_timing_admission():
    from domain.services.print_cards.comparison import comparison_summary

    record = {"metadata_json": {"prediction": {"print_hours": 1, "layer_count": 1}}}
    session = {"classification": "REAL_PRINT"}
    rows = [(1, {"burn_ms": 30_000, "pour_ms": 9_000, "make_layer_ms": 39_300}, None)]
    result, calls = _count_timing_admissions(lambda: comparison_summary(record, session, rows))
    assert result["comparison_status"] == "comparable"
    assert result["actual_source"] == "subtotal_machine_log"
    assert calls == 1


def test_calibration_projections_use_one_timing_admission_per_session():
    from analytics.prediction.calibration_inputs import CalibrationInputs

    rows = [
        ("complete", 1, {"burn_ms": 30_000, "pour_ms": 9_000, "make_layer_ms": 39_300}),
        ("retry", 1, {"burn_ms": 30_000, "pour_ms": 9_000, "make_layer_ms": 39_300}),
        ("retry", 1, {"burn_ms": 50_000, "pour_ms": 9_000, "make_layer_ms": 59_300}),
        ("burn_only", 1, {"burn_ms": 30_000}),
        ("small_burn", 1, {"burn_ms": 50, "pour_ms": 1_000, "make_layer_ms": 1_050}),
        ("large_burn", 1, {"burn_ms": 3_600_001, "pour_ms": 1_000, "make_layer_ms": 3_601_001}),
    ]
    inputs = CalibrationInputs([], rows, None, {
        "empty": {"schema": 1, "publication_id": "empty", "row_count": 0,
                  "rows_fingerprint": stable_hash([]), "status": "empty"},
        "invalid": {"schema": 99},
    })
    before = deepcopy((inputs.timing_rows, inputs.timing_publications))
    fingerprint = inputs.input_fingerprint
    projections, calls = _count_timing_admissions(lambda: (inputs.components, inputs.burns, inputs.cycles))
    components, burns, cycles = projections
    for values in projections:
        assert set(values) == {"complete", "retry", "burn_only", "small_burn", "large_burn", "empty", "invalid"}
        assert values["retry"] == values["empty"] == values["invalid"] == {}
    assert components["complete"] == {1: (30.0, 9.0)}
    assert burns["complete"] == burns["burn_only"] == {1: 30.0}
    assert cycles["complete"] == {1: (30_000.0, 9_000.0, 39_300.0)}
    assert components["small_burn"] == {1: (0.05, 1.0)}
    assert components["large_burn"] == {1: (3_600.001, 1.0)}
    assert burns["small_burn"] == burns["large_burn"] == {}
    assert cycles["small_burn"] == cycles["large_burn"] == cycles["burn_only"] == {}
    assert components["burn_only"] == {}
    assert (inputs.timing_rows, inputs.timing_publications) == before
    assert inputs.input_fingerprint == fingerprint
    assert calls == 7


@pytest.mark.parametrize("case, expected_status", [
    ("complete", "complete"), ("legacy", "legacy"), ("absent", "absent"),
    ("empty", "empty"), ("no_time_log", "no_time_log"),
    ("wrong_schema", "invalid"), ("wrong_count", "invalid"),
    ("wrong_tag", "invalid"), ("missing_manifest", "invalid"),
    ("changed_value", "invalid"), ("duplicate_layer", "invalid"),
    ("negative_layer", "invalid"), ("invalid_features", "invalid"),
    ("invalid_status", "invalid"), ("empty_complete", "invalid"),
    ("known_bad_timing", "complete"),
])
def test_publication_returns_consistent_status_and_events_without_mutating_inputs(case, expected_status):
    payload = event()["payload"]
    rows = [(1, {key: value for key, value in payload.items() if key != "layer"}, "publication")]
    if case == "known_bad_timing":
        rows[0][1]["timing_valid"] = False
    manifest = {"schema": 1, "publication_id": "publication", "status": "complete",
                "row_count": 1, "rows_fingerprint": stable_hash([
                    {"layer": layer, "features": features} for layer, features, _ in rows])}
    if case == "legacy":
        rows = [(layer, features, None) for layer, features, _ in rows]
        manifest = None
    elif case in {"absent", "empty", "no_time_log", "empty_complete"}:
        rows = []
        manifest = None if case == "absent" else {
            **manifest, "row_count": 0, "rows_fingerprint": stable_hash([]),
            "status": "complete" if case == "empty_complete" else case,
        }
    elif case == "wrong_schema":
        manifest["schema"] = 99
    elif case == "wrong_count":
        manifest["row_count"] = 2
    elif case == "wrong_tag":
        rows = [(1, rows[0][1], "different-publication")]
    elif case == "missing_manifest":
        manifest = None
    elif case == "changed_value":
        rows[0][1]["burn_ms"] += 1
    elif case == "duplicate_layer":
        rows.append(deepcopy(rows[0]))
        manifest["row_count"] = 2
        manifest["rows_fingerprint"] = stable_hash([
            {"layer": layer, "features": features} for layer, features, _ in rows])
    elif case == "negative_layer":
        rows = [(-1, rows[0][1], "publication")]
    elif case == "invalid_features":
        rows = [(1, None, "publication")]
    elif case == "invalid_status":
        manifest["status"] = "empty"
    before = deepcopy((rows, manifest))
    status, events = read_timing_publication(rows, manifest)
    assert status == expected_status
    if case == "absent":
        assert events is None
    elif status in {"invalid", "empty", "no_time_log"}:
        assert events == []
    else:
        assert events == [{"event_type": "layer_timing_summary", "payload": {
            **rows[0][1], "layer": 1,
        }}]
        if case == "known_bad_timing":
            assert calibration_timing_payloads(events) == {}
    assert (rows, manifest) == before
