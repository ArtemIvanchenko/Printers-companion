import pytest

from analytics.prediction.timing_evidence import summarize_timing_events


def _event(layer: int, burn: int, pour: int, make: int) -> dict:
    return {
        "event_type": "layer_timing_summary",
        "payload": {
            "layer": layer,
            "burn_ms": burn,
            "pour_ms": pour,
            "make_layer_ms": make,
        },
    }


def test_full_cycle_keeps_controller_overhead() -> None:
    evidence = summarize_timing_events([
        _event(1, 10_000, 8_000, 18_500),
        _event(2, 12_000, 8_000, 20_250),
    ])

    hours = evidence.component_hours()
    assert hours["controller_overhead"] == pytest.approx(750 / 3_600_000)
    assert hours["full_machine_cycle"] == pytest.approx(38_750 / 3_600_000)
    assert evidence.coverage_ratio == 1.0


def test_repeated_attempt_is_first_wins_for_calibration_but_counted_in_actual_work() -> None:
    evidence = summarize_timing_events([
        _event(2, 10_000, 8_000, 18_250),
        _event(3, 11_000, 8_000, 19_250),
        _event(3, 20_000, 8_000, 28_250),
        _event(4, 12_000, 8_000, 20_250),
    ])

    assert evidence.observed_layers == 3
    assert evidence.duplicate_rows == 1
    assert evidence.conflicting_duplicates == 1
    assert evidence.repeated_attempt_rows == 1
    assert evidence.equivalent_duplicate_rows == 0
    assert evidence.ambiguous_layers == frozenset({3})
    assert len(evidence.attempts) == 4
    assert evidence.cycles[3].burn_ms == 11_000
    assert evidence.missing_layer_count == 1


def test_equivalent_stitched_boundary_is_not_counted_as_machine_work_twice() -> None:
    row = _event(384, 10_000, 8_000, 18_250)
    evidence = summarize_timing_events([row, row])

    assert evidence.duplicate_rows == 1
    assert evidence.equivalent_duplicate_rows == 1
    assert evidence.repeated_attempt_rows == 0
    assert len(evidence.attempts) == 1
    assert evidence.component_hours()["all_attempts_full_cycle"] == pytest.approx(
        18_250 / 3_600_000,
    )


def test_long_in_layer_stop_is_separate_from_nominal_machine_cycle() -> None:
    evidence = summarize_timing_events([
        _event(1, 10_000, 8_000, 18_400),
        _event(2, 10_000, 8_000, 3_618_400),
    ])

    hours = evidence.component_hours()
    assert hours["full_machine_cycle"] == pytest.approx(3_636_800 / 3_600_000)
    assert hours["pause_like_residual"] == pytest.approx(1.0)
    assert hours["nominal_cycle_without_pause_like_residual"] == pytest.approx(
        36_800 / 3_600_000,
    )


def test_non_timing_and_invalid_rows_do_not_enter_fact() -> None:
    evidence = summarize_timing_events([
        {"event_type": "burn_start", "payload": {"layer": 1}},
        _event(0, 1, 1, 2),
        _event(1, -1, 1, 2),
        _event(2, 10_000, 8_000, 1_000),
    ])

    assert evidence.observed_layers == 0
    assert evidence.invalid_rows == 3
