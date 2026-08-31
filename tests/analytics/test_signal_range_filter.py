"""Firmware garbage must not reach the statistics — and a wrong profile range
must not eat real data.

The printer writes finite-but-impossible values into sensors.log (a Flow H of
-2.58e18 %, an SP15 of 9.5e26 bar). They pass every nan/inf guard and destroy
any mean, std or trend computed over them: 33 such rows out of 81 377 pulled
this shop's real Flow H mean to 1.9e23, which then reached the maintenance
forecast as a genuine reading.
"""
import pytest

from analytics.telemetry_parser import compute_full_signal_stats, downsample_full_series

_HEADER = ("      Time|       LIR|       ST4|       ST3|       ST5|"
           "    Flow T|    Flow H|       SO1|       SO2|       SF1|\n")


def _log(tmp_path, rows: list[dict], name: str = "01.01.2026_sensors.log"):
    """Write a sensors.log with the real pipe-delimited layout."""
    cols = ["LIR", "ST4", "ST3", "ST5", "Flow T", "Flow H", "SO1", "SO2", "SF1"]
    path = tmp_path / name
    with path.open("w", encoding="utf-8") as fh:
        fh.write(_HEADER)
        for i, row in enumerate(rows):
            cells = "|".join(f"{row.get(c, 0.0):>10}" for c in cols)
            fh.write(f"  {i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}|{cells}|\n")
    return path


def test_firmware_garbage_excluded_from_mean(tmp_path):
    """One impossible sample must not move the mean."""
    rows = [{"Flow H": 7.0}] * 60 + [{"Flow H": -2583060676901601280.0}]
    stats = compute_full_signal_stats(_log(tmp_path, rows))

    assert stats["Flow H"]["mean"] == pytest.approx(7.0)
    assert stats["Flow H"]["out_of_range"] == 1
    assert stats["Flow H"]["n"] == 60


def test_garbage_is_counted_not_silently_dropped(tmp_path):
    """A failing sensor has to stay visible, so the count is reported."""
    rows = [{"SO1": 0.5}] * 50 + [{"SO1": 9.5e26}] * 5
    stats = compute_full_signal_stats(_log(tmp_path, rows))

    assert stats["SO1"]["out_of_range"] == 5
    assert stats["SO1"]["max"] == pytest.approx(0.5)


def test_clean_signal_reports_no_rejects(tmp_path):
    rows = [{"ST5": 40.0 + i * 0.01} for i in range(60)]
    stats = compute_full_signal_stats(_log(tmp_path, rows))

    assert stats["ST5"]["out_of_range"] == 0
    assert stats["ST5"]["n"] == 60


def test_absurd_filter_applies_without_any_profile_range(tmp_path):
    """SP14/SP15 carry no min_val/max_val in the profile, yet logged 4.6e28."""
    rows = [{"ST3": 25.0}] * 50 + [{"ST3": 4.5e28}] * 3
    stats = compute_full_signal_stats(_log(tmp_path, rows), valid_ranges={})

    assert stats["ST3"]["mean"] == pytest.approx(25.0)
    assert stats["ST3"]["out_of_range"] == 3


def test_downsample_drops_finite_firmware_garbage(tmp_path):
    rows = [{"Flow H": 7.0}] * 9 + [{"Flow H": -2.58e18}]
    sampled = downsample_full_series(_log(tmp_path, rows), ["Flow H"], max_points=10)
    assert sampled["Flow H"][-1] is None


def test_downsample_can_clip_to_active_clock_window(tmp_path):
    rows = [{"SO1": float(i)} for i in range(60)]
    sampled = downsample_full_series(
        _log(tmp_path, rows), ["SO1"], max_points=60,
        start_clock_seconds=10, end_clock_seconds=19,
        valid_ranges={},
    )
    assert sampled["SO1"] == [float(i) for i in range(10, 20)]


class TestWrongProfileRangeIsIgnored:
    """A range rejecting most of a signal describes a different machine.

    Real cases: LIR reads negative throughout while the profile says
    0..390000 (99.9% rejected), SF1 reads ~986 against a stated 0..30 (54%).
    Both are profile guesses — confidence 0.5, active_status "candidate".
    """

    def test_range_rejecting_almost_everything_is_not_applied(self, tmp_path):
        rows = [{"LIR": -95628.0}] * 60
        stats = compute_full_signal_stats(
            _log(tmp_path, rows), valid_ranges={"LIR": {"min_val": 0.0, "max_val": 390000.0}},
        )

        assert stats["LIR"]["n"] == 60
        assert stats["LIR"]["out_of_range"] == 0
        assert stats["LIR"]["mean"] == pytest.approx(-95628.0)

    def test_absurd_values_still_dropped_when_profile_range_is_ignored(self, tmp_path):
        """Rejecting the bad range must not also disable the absolute guard."""
        rows = [{"LIR": -95628.0}] * 60 + [{"LIR": 1.87e9}]
        stats = compute_full_signal_stats(
            _log(tmp_path, rows), valid_ranges={"LIR": {"min_val": 0.0, "max_val": 390000.0}},
        )

        assert stats["LIR"]["out_of_range"] == 1
        assert stats["LIR"]["max"] == pytest.approx(-95628.0)

    def test_plausible_range_is_still_enforced(self, tmp_path):
        """The escape hatch must not make every range advisory."""
        rows = [{"SO1": 0.5}] * 58 + [{"SO1": 50.0}] * 2
        stats = compute_full_signal_stats(
            _log(tmp_path, rows), valid_ranges={"SO1": {"min_val": 0.0, "max_val": 21.0}},
        )

        assert stats["SO1"]["out_of_range"] == 2
        assert stats["SO1"]["max"] == pytest.approx(0.5)
