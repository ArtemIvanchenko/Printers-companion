from itertools import permutations

import pytest

from analytics.prediction.recoat_calibration import layer_seconds_from_events
from analytics.prediction.scan_calibration import _burn_seconds_by_layer, _layer_cycles_ms_by_layer
from analytics.prediction.timing_evidence import summarize_timing_events
from analytics.prediction.timing_validation import calibration_timing_payloads


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
