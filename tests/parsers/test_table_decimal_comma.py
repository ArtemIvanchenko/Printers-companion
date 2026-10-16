import csv
from collections import Counter
from hashlib import sha256
from types import SimpleNamespace

import pytest

from analytics.log_insights.environment import sensor_samples
from parsers.base.base import ParserContext
from parsers.formats._tables import parse_table_stream
from parsers.formats.burn_log import BurnLogParser
from parsers.formats.sensors_log import SensorsLogParser
from parsers.formats.stateflow_log import StateFlowLogParser
from parsers.formats.table_temp_log import TableTempLogParser


@pytest.mark.parametrize("separator", [" ", "  ", "\t", ";", "|"])
def test_header_separator_keeps_decimal_commas_in_both_sensor_paths(tmp_path, separator):
    path = tmp_path / "08.06.2026_sensors.log"
    path.write_text("\n".join(separator.join(row) for row in [
        ["Time", "SO1", "SO2"],
        ["23:59:59", "0,1", "0,2"],
        ["Time", "SO1", "SO2"],
        ["00:00:01", "0,3", "0,4"],
    ]) + "\n")
    table, diagnostics, metadata = parse_table_stream(path, known_columns=["Time", "SO1", "SO2"])
    assert table.rows == [
        {"Time": "23:59:59", "SO1": 0.1, "SO2": 0.2},
        {"Time": "00:00:01", "SO1": 0.3, "SO2": 0.4},
    ]
    assert diagnostics == []
    assert metadata["malformed_rows"] == 0
    assert metadata["repeated_headers"] == 1
    source = SimpleNamespace(path=str(path), checksum=sha256(path.read_bytes()).hexdigest(),
                             classification=SimpleNamespace(family="sensors_log"))
    stream_diagnostics = Counter()
    rows = list(sensor_samples([source], {"SO1": {}, "SO2": {}}, stream_diagnostics,
                               apply_profile_ranges=False))
    assert [values for _, values in rows] == [{"SO1": 0.1, "SO2": 0.2}, {"SO1": 0.3, "SO2": 0.4}]
    assert rows[1][0] - rows[0][0] == 2
    assert stream_diagnostics["midnight_rollovers"] == 1
    assert stream_diagnostics["repeated_headers"] == 1


@pytest.mark.parametrize("parser_class", [SensorsLogParser, BurnLogParser, TableTempLogParser])
def test_table_parser_consumers_share_the_header_separator(tmp_path, parser_class):
    path = tmp_path / "08.06.2026_sensors.log"
    path.write_text("Time SO1 SO2\n12:00:00 0,1 0,2\n")
    result = parser_class().parse(path, ParserContext(signal_mappings={"SO1": {}, "SO2": {}}))
    assert result.tables[0].rows == [{"Time": "12:00:00", "SO1": 0.1, "SO2": 0.2}]
    assert result.tables[0].unknown_columns == []


def test_stateflow_keeps_fractional_states_and_timestamp_columns(tmp_path):
    path = tmp_path / "08.06.2026_stateFlow.log"
    path.write_text("Time State Mystery\n12:00:00 0,1 0,2\n12:00:01 0,3 0,4\n")
    result = StateFlowLogParser().parse(path, ParserContext())
    [transition] = result.transitions
    assert transition.previous_state == {"State": 0.1, "Mystery": 0.2}
    assert transition.new_state == {"State": 0.3, "Mystery": 0.4}
    assert transition.duration_sec == 1
    assert result.metadata["missing_timestamps"] == 0


def test_broken_row_separator_is_not_silently_reinterpreted_as_csv(tmp_path):
    path = tmp_path / "08.06.2026_sensors.log"
    path.write_text("Time;SO1;SO2\n12:00:00 0,1 0,2\n12:00:01;0,3;0,4\n")
    table, _, metadata = parse_table_stream(path)
    assert table.rows[0] == {"Time": "12:00:00 0,1 0,2", "SO1": None, "SO2": None}
    assert table.rows[1] == {"Time": "12:00:01", "SO1": 0.3, "SO2": 0.4}
    assert metadata["malformed_rows"] == 1


def test_comma_delimited_quoted_values_still_use_csv_rules(tmp_path):
    path = tmp_path / "08.06.2026_sensors.log"
    path.write_text('Time,SO1,SO2\n12:00:00,"0,1","0,2"\n')
    table, _, metadata = parse_table_stream(path)
    assert table.rows == [{"Time": "12:00:00", "SO1": 0.1, "SO2": 0.2}]
    assert metadata["malformed_rows"] == 0


@pytest.mark.parametrize("separator", [";", ",", "\t"])
def test_literal_pipe_in_unknown_cell_does_not_replace_header_separator(tmp_path, separator):
    path = tmp_path / "08.06.2026_sensors.log"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter=separator)
        writer.writerows([["Time", "SO1", "Unknown"], ["12:00:00", "0,1", "opaque|value"]])
    table, _, metadata = parse_table_stream(path, known_columns=["Time", "SO1"])
    assert table.rows == [{"Time": "12:00:00", "SO1": 0.1, "Unknown": "opaque|value"}]
    assert table.unknown_columns == ["Unknown"]
    assert metadata["malformed_rows"] == 0
