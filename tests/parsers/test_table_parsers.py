from pathlib import Path

from parsers.base.base import ParserContext
from parsers.formats.burn_log import BurnLogParser
from parsers.formats.sensors_log import SensorsLogParser
from parsers.formats._tables import build_header, parse_table_stream


def test_burn_log_ignores_repeated_headers_and_preserves_unknown_columns(tmp_path: Path) -> None:
    path = tmp_path / "job_burn.log"
    path.write_text("Layer;SO1;Mystery\nLayer;SO1;Mystery\n1;10;abc\nbad;row\n", encoding="utf-8")
    result = BurnLogParser().parse(path, ParserContext(signal_mappings={"SO1": {}}))
    table = result.tables[0]

    assert table.repeated_headers == 1
    assert "Mystery" in table.unknown_columns
    assert table.malformed_rows == 1
    assert any(diag.code == "repeated_headers_ignored" for diag in result.diagnostics)


def test_sensors_log_marks_startup_garbage_without_process_anomaly(tmp_path: Path) -> None:
    path = tmp_path / "job_sensors.log"
    path.write_text("t;SO1\n0;999999999\n1;12\n", encoding="utf-8")
    result = SensorsLogParser().parse(path, ParserContext())

    assert result.metadata["startup_bad_rows"] == 1
    assert any(diag.code == "startup_telemetry_garbage" for diag in result.diagnostics)
    assert result.tables[0].rows[0]["SO1"] is None


def test_table_sampler_covers_tail_and_audits_rows_after_sample_limit(tmp_path: Path) -> None:
    path = tmp_path / "long_sensors.log"
    lines = ["Time|SO1", *[f"00:00:{i % 60:02d}|{i}" for i in range(200)]]
    lines += ["Time|SO1", "late|row|with|bad|width"]
    path.write_text("\n".join(lines), encoding="utf-8")

    table, diagnostics, metadata = parse_table_stream(path, max_rows=20)

    assert len(table.rows) == 20
    assert metadata["sample_strategy"] == "head_plus_deterministic_reservoir"
    assert metadata["sample_last_row"] == metadata["total_rows"] - 1
    assert table.repeated_headers == 1
    assert table.malformed_rows == 1
    assert any(item.code == "malformed_row" for item in diagnostics)


def test_header_names_are_unique_when_firmware_repeats_a_column() -> None:
    assert build_header(["Time", "SP1", "SP1", ""]) == ["Time", "SP1", "SP1_2", "col_3"]
