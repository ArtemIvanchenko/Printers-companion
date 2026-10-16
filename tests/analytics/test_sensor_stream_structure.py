from collections import Counter
from datetime import datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest

from analytics.log_insights.clocks import seconds
from analytics.log_insights.environment import sensor_samples
from domain.services.session_telemetry import _layer_time_points, analysis_telemetry, chart_telemetry
from parsers.formats._tables import build_header, parse_table_stream


def source_file(tmp_path, text, name="18.07.2026_sensors.log"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return SimpleNamespace(
        path=str(path), classification=SimpleNamespace(family="sensors_log"),
        checksum=sha256(path.read_bytes()).hexdigest(), parse_result=None,
    )


@pytest.mark.parametrize("separator", ["|", ";", "\t", ",", " "])
def test_full_reader_uses_the_same_table_delimiters(tmp_path, separator):
    lines = [separator.join(row) for row in [
        ["Time", "SO1", "Unknown"], ["12:00:00", "0.1", "a"], ["12:00:01", "0.2", "b"],
    ]]
    source = source_file(tmp_path, "\n".join(lines))
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    table, _, _ = parse_table_stream(source_file_path(source), known_columns=["Time", "SO1"])
    assert len(rows) == len(table.rows) == 2
    assert [row[1]["SO1"] for row in rows] == [row["SO1"] for row in table.rows]
    assert table.unknown_columns == ["Unknown"]
    assert diagnostics["total_rows"] == 2


def source_file_path(source):
    from pathlib import Path
    return Path(source.path)


def test_duplicate_header_suffix_never_overwrites_native_columns():
    names = build_header(["Time", "SO1", "SO1", "SO1_2", ""])
    assert len(names) == len(set(names))
    assert names[1] == "SO1"
    assert names[3] == "SO1_2"


def test_full_reader_matches_duplicate_header_indices_and_keeps_source_mapping(tmp_path):
    source = source_file(tmp_path, "Time|SO1|SO1|SO1_2|Mystery|\n12:00:00|0.1|9|4|opaque|\n")
    table, _, _ = parse_table_stream(source_file_path(source), known_columns=["Time", "SO1"])
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    assert rows[0][1]["SO1"] == table.rows[0]["SO1"] == 0.1
    assert sorted(value for key, value in table.rows[0].items() if key.startswith("SO1")) == [0.1, 4, 9]
    assert table.rows[0]["Mystery"] == "opaque"
    sources = diagnostics["source_columns"]
    columns = next(iter(sources.values()))
    assert columns[1] == {"index": 1, "raw_name": "SO1", "column": "SO1"}
    assert columns[2]["index"] == 2 and columns[2]["raw_name"] == "SO1"
    assert columns[3]["column"] == "SO1_2"
    assert columns[4]["raw_name"] == "Mystery"


@pytest.mark.parametrize("bad_row", ["12:00:00|0.1|", "12:00:00|0.1|x|overflow|"])
def test_ragged_stream_is_audited_without_losing_valid_cells(tmp_path, bad_row):
    source = source_file(tmp_path, f"Time|SO1|Unknown|\n{bad_row}\n12:00:01|0.2|a|\n")
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    assert [row[1]["SO1"] for row in rows] == [0.1, 0.2]
    assert diagnostics["malformed_rows"] == 1
    _, _, evidence = analysis_telemetry([source])
    assert evidence["sample_count"] == 2
    assert evidence["complete_available_stream"] is False
    assert evidence["read_diagnostics"]["malformed_rows"] == 1


def test_missing_time_column_does_not_silently_drop_a_source(tmp_path):
    source = source_file(tmp_path, "Other|SO1|\ntext|0.1|\n")
    diagnostics = Counter()
    assert list(sensor_samples([source], {"SO1": {}}, diagnostics)) == []
    assert diagnostics["missing_time_columns"] == 1
    assert diagnostics["unparsed_rows"] == 1
    assert diagnostics["total_rows"] == 1


def test_bad_timestamp_rows_prevent_stream_completeness(tmp_path):
    source = source_file(tmp_path,
        "Time|SO1|\n12:00:00|0.1|\n24:00:00|0.2|\n2026-02-31 12:00:02|0.3|\n")
    telemetry, _, evidence = analysis_telemetry([source])
    assert len(telemetry["timestamps"]) == 1
    assert evidence["complete_available_stream"] is False
    assert evidence["read_diagnostics"]["invalid_timestamps"] == 2
    assert evidence["read_diagnostics"]["total_rows"] == 3


def test_long_fraction_clock_rolls_over_without_length_heuristic(tmp_path):
    source = source_file(tmp_path, "Time|SO1|\n23:59:59.0000010000|0.1|\n00:00:01.0000010000|0.2|\n")
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    assert rows[1][0] - rows[0][0] == pytest.approx(2)
    assert diagnostics["midnight_rollovers"] == 1


def test_explicit_and_backward_dates_share_forward_calendar_anchor(tmp_path):
    source = source_file(tmp_path,
        "Time|SO1|\n2026-07-20 23:59:59|0.1|\n2026-07-18 12:00:00|0.2|\n00:00:01|0.3|\n")
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    assert rows[-1][0] == seconds(datetime(2026, 7, 21, 0, 0, 1))
    assert diagnostics["out_of_order_timestamps"] == 1
    assert diagnostics["midnight_rollovers"] == 1


def test_reader_keeps_first_duplicate_time_column(tmp_path):
    source = source_file(tmp_path, "Time|Time|SO1|\n12:00:00|broken|0.1|\n")
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    assert len(rows) == 1
    assert rows[0][0] == seconds(datetime(2026, 7, 18, 12))


def test_header_repeats_do_not_consume_rows_or_reset_calendar(tmp_path):
    source = source_file(tmp_path,
        "Time|SO1|\n23:59:59|0.1|\nTime|SO1|\n00:00:01|0.2|\n")
    diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}}, diagnostics, apply_profile_ranges=False))
    assert len(rows) == 2
    assert diagnostics["repeated_headers"] == 1
    assert diagnostics["total_rows"] == 2
    assert diagnostics["midnight_rollovers"] == 1


@pytest.mark.parametrize("raw", ["24:00:00", "12:60:00", "12:01:60", "12:01:nan", "12:01:inf"])
def test_fallback_clock_never_normalizes_invalid_clock_fields(tmp_path, raw):
    source = source_file(tmp_path, f"Time|N|\n{raw}|1|\n", name="18.07.2026_burn.log")
    source.classification.family = "burn_log"
    table, _, _ = parse_table_stream(source_file_path(source))
    source.parse_result = SimpleNamespace(file_family="burn_log", tables=[table])
    assert _layer_time_points([source]) == []


def test_chart_projection_cannot_change_full_stream_quality_or_sample_count(tmp_path):
    source = source_file(tmp_path,
        "Time|SO1|Unknown|\n" + "".join(f"12:00:{i:02d}|0.1|a|\n" for i in range(20)) +
        "12:00:20|0.2|\n")
    telemetry, _, evidence = analysis_telemetry([source])
    display = chart_telemetry(telemetry, limit=3)
    assert len(telemetry["time"]) == evidence["sample_count"] == 21
    assert len(display["time"]) == 3
    assert evidence["complete_available_stream"] is False
    assert display["timestamp_diagnostics"] == telemetry["timestamp_diagnostics"]
