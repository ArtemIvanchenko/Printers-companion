"""Tests for analytics.data_quality.assess_session_quality."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from analytics.data_quality import assess_session_quality
from domain.enums.common import SourceFileFamily

UTC = timezone.utc


def _ev(layer=None, ts=None):
    return SimpleNamespace(layer=layer, ts=ts)


def _file(family, events=None, transitions=None, tables=None, diagnostics=None):
    pr = SimpleNamespace(
        file_family=family,
        events=events or [],
        transitions=transitions or [],
        tables=tables or [],
        diagnostics=diagnostics or [],
    )
    return SimpleNamespace(parse_result=pr)


def _full_print_files(events):
    """A complete family set so missing_log_family never fires by accident."""
    return [
        _file(SourceFileFamily.main_event_log, events=events),
        _file(SourceFileFamily.burn_log),
        _file(SourceFileFamily.sensors_log),
        _file(SourceFileFamily.time_log),
    ]


def test_clean_session_scores_high():
    events = [_ev(layer=i, ts=datetime(2026, 3, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=i))
              for i in range(1, 50)]
    files = _full_print_files(events)
    stats = {"SO1": {"n": 1000, "std": 0.04}, "SO2": {"n": 1000, "std": 0.03}}
    result = assess_session_quality(files, events, {}, stats)
    assert result["score"] >= 85
    assert result["grade"] == "good"
    assert result["issues"] == []


def test_layer_gaps_flagged():
    # layers 1..40 but drop 10..25 → big gap
    layers = list(range(1, 10)) + list(range(26, 41))
    events = [_ev(layer=n, ts=datetime(2026, 3, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=n))
              for n in layers]
    files = _full_print_files(events)
    result = assess_session_quality(files, events, {}, {})
    kinds = {i["kind"] for i in result["issues"]}
    assert "layer_gaps" in kinds
    gap = next(i for i in result["issues"] if i["kind"] == "layer_gaps")
    assert gap["count"] == 16
    assert result["score"] < 85


def test_time_gap_flagged():
    base = datetime(2026, 3, 23, 16, 0, tzinfo=UTC)
    times = [base + timedelta(minutes=i) for i in range(5)]
    times += [base + timedelta(hours=3) + timedelta(minutes=i) for i in range(5)]  # 3h gap
    events = [_ev(layer=i, ts=t) for i, t in enumerate(times, start=1)]
    files = _full_print_files(events)
    result = assess_session_quality(files, events, {}, {})
    kinds = {i["kind"] for i in result["issues"]}
    assert "time_gaps" in kinds
    gap = next(i for i in result["issues"] if i["kind"] == "time_gaps")
    assert gap["max_gap_min"] > 120


def test_monitor100_does_not_trigger_time_gap():
    # monitor100 daemon spans the whole day; must be excluded from gap check.
    base = datetime(2026, 3, 23, 16, 0, tzinfo=UTC)
    events = [_ev(layer=i, ts=base + timedelta(seconds=i)) for i in range(1, 30)]
    mon_events = [_ev(ts=datetime(2026, 3, 23, 3, 0, tzinfo=UTC)),
                  _ev(ts=datetime(2026, 3, 23, 23, 0, tzinfo=UTC))]
    files = _full_print_files(events) + [_file(SourceFileFamily.monitor100_log, events=mon_events)]
    result = assess_session_quality(files, events, {}, {})
    assert "time_gaps" not in {i["kind"] for i in result["issues"]}


def test_sensor_stuck_flagged():
    events = [_ev(layer=i, ts=datetime(2026, 3, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=i))
              for i in range(1, 20)]
    files = _full_print_files(events)
    stats = {"SO1": {"n": 1000, "std": 0.0}, "SO2": {"n": 1000, "std": 0.05}}
    result = assess_session_quality(files, events, {}, stats)
    stuck = next((i for i in result["issues"] if i["kind"] == "sensor_stuck"), None)
    assert stuck is not None
    assert "SO1" in stuck["signals"]


def test_sensor_dropout_flagged():
    events = [_ev(layer=i, ts=datetime(2026, 3, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=i))
              for i in range(1, 20)]
    files = _full_print_files(events)
    stats = {"SO1": {"n": 1000, "std": 0.04}, "SO2": {"n": 1000, "std": 0.03},
             "ST3": {"n": 10, "std": 0.5}}  # far fewer samples
    result = assess_session_quality(files, events, {}, stats)
    drop = next((i for i in result["issues"] if i["kind"] == "sensor_dropout"), None)
    assert drop is not None
    assert "ST3" in drop["signals"]


def test_missing_log_family_flagged():
    events = [_ev(layer=i, ts=datetime(2026, 3, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=i))
              for i in range(1, 20)]
    # event + sensors present (2 of expected) but time_log missing
    files = [_file(SourceFileFamily.main_event_log, events=events),
             _file(SourceFileFamily.sensors_log)]
    result = assess_session_quality(files, events, {}, {})
    missing = next((i for i in result["issues"] if i["kind"] == "missing_log_family"), None)
    assert missing is not None
    assert "time_log" in missing["families"]


def test_parse_warnings_flagged():
    events = [_ev(layer=i, ts=datetime(2026, 3, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=i))
              for i in range(1, 20)]
    bad_table = SimpleNamespace(malformed_rows=42, unknown_columns=["XYZ"])
    files = _full_print_files(events)
    files[0].parse_result.tables = [bad_table]
    result = assess_session_quality(files, events, {}, {})
    warn = next((i for i in result["issues"] if i["kind"] == "parse_warnings"), None)
    assert warn is not None
    assert warn["malformed_rows"] == 42


def test_empty_session_does_not_crash():
    result = assess_session_quality([], [], {}, {})
    assert result["score"] == 100.0
    assert result["issues"] == []
