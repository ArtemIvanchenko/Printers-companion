from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult
from parsers.base.base import BaseParser, ParserContext
from parsers.formats._tables import parse_table_stream


class SensorsLogParser(BaseParser):
    name = "sensors_log"
    version = "0.1.0"
    file_family = SourceFileFamily.sensors_log
    role = FileRole.secondary

    # Columns that are always present in sensors.log but are not signals —
    # the timestamp column and powder-system counters are expected, not unknown.
    _META_COLUMNS = frozenset({"Time"})

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        known = set(context.signal_mappings.keys()) | self._META_COLUMNS
        table, diagnostics, metadata = parse_table_stream(
            path,
            known_columns=known,
            max_rows=int(context.options.get("sensor_sample_rows", 5000)),
        )
        startup_bad_rows = 0
        for row in table.rows[: int(context.options.get("startup_window_rows", 100))]:
            numeric_values = [value for value in row.values() if isinstance(value, int | float)]
            if any(abs(float(value)) > 1_000_000 for value in numeric_values):
                startup_bad_rows += 1
        if startup_bad_rows:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="startup_telemetry_garbage",
                    message="Extreme sensor values were confined to the startup sample window.",
                    context={"startup_bad_rows": startup_bad_rows},
                )
            )
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            tables=[table],
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if table.malformed_rows else ["ok"],
            metadata=metadata | {"startup_bad_rows": startup_bad_rows},
        )

