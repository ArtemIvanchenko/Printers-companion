from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult
from parsers.base.base import BaseParser, ParserContext
from parsers.common.numeric_quality import neural_reconstruction_profile, robust_numeric_profile
from parsers.formats._tables import parse_table_stream


class SensorsLogParser(BaseParser):
    name = "sensors_log"
    version = "0.2.0"
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
            numeric_abs_limit=float(context.options.get("sensor_abs_limit", 10_000_000)),
            startup_window_rows=int(context.options.get("startup_window_rows", 100)),
        )
        startup_bad_rows = int(metadata.get("startup_invalid_rows", 0))
        if startup_bad_rows:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="startup_telemetry_garbage",
                    message="Extreme sensor values were filtered in the startup sample window.",
                    context={
                        "startup_bad_rows": startup_bad_rows,
                        "filtered_cells_in_sample": metadata["sampled_invalid_numeric_cells"],
                    },
                )
            )
        robust_profile = robust_numeric_profile(table.rows, ignored_columns={"Time"})
        neural_enabled = bool(context.options.get("enable_neural_log_analysis", False))
        neural_profile = (
            neural_reconstruction_profile(
                table.rows,
                ignored_columns={"Time"},
                startup_rows=int(context.options.get("startup_window_rows", 100)),
            )
            if neural_enabled else {"status": "disabled"}
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
            metadata=metadata | {
                "startup_bad_rows": startup_bad_rows,
                "numeric_abs_limit": float(context.options.get("sensor_abs_limit", 10_000_000)),
                "numeric_quality": robust_profile,
                "neural_quality": neural_profile,
            },
        )
