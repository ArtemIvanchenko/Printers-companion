from pathlib import Path

from parsers.base.base import ParserContext
from parsers.formats.burn_log import BurnLogParser
from parsers.formats.sensors_log import SensorsLogParser


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

