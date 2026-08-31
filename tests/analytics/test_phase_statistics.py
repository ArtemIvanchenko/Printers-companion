from domain.schemas.parsing import CanonicalEventDraft
from analytics.phase_statistics import compute_layer_phase_statistics


def _layer(layer: int, pour_ms: int, burn_ms: int, make_ms: int) -> CanonicalEventDraft:
    return CanonicalEventDraft(
        layer=layer,
        event_type="layer_timing_summary",
        phase="layer",
        payload={
            "layer": layer,
            "pour_ms": pour_ms,
            "burn_ms": burn_ms,
            "make_layer_ms": make_ms,
        },
    )


def test_phase_statistics_use_machine_layer_truth_and_first_duplicate():
    events = [
        _layer(1, 9000, 21000, 32000),
        _layer(1, 999999, 999999, 999999),
        _layer(2, 10000, 22000, 35000),
        _layer(3, 11000, 23000, 39000),
    ]
    result = compute_layer_phase_statistics(events)

    assert result["available"] is True
    assert result["layer_count"] == 3
    assert result["phases"]["laser_scan"]["total_sec"] == 66
    assert result["phases"]["powder_recoat"]["total_sec"] == 30
    assert result["phases"]["controller_overhead"]["total_sec"] == 10
    assert result["phases"]["full_layer_cycle"]["total_sec"] == 106
