from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult
from parsers.base.base import BaseParser, ParserContext
from parsers.formats._tables import parse_table_stream


class BurnLogParser(BaseParser):
    name = "burn_log"
    version = "0.2.0"
    file_family = SourceFileFamily.burn_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        known_columns = set(context.signal_mappings) | {"Time", "N", "LIR", "Table"}
        table, diagnostics, metadata = parse_table_stream(
            path,
            known_columns=known_columns,
            numeric_abs_limit=float(context.options.get("sensor_abs_limit", 10_000_000)),
        )
        if table.repeated_headers:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="info",
                    code="repeated_headers_ignored",
                    message=f"Ignored {table.repeated_headers} repeated burn-log header rows.",
                    context={"count": table.repeated_headers},
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
            metadata=metadata | {"unknown_columns": table.unknown_columns},
        )
