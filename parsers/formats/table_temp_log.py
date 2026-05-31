from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseResult
from parsers.base.base import BaseParser, ParserContext
from parsers.formats._tables import parse_table_stream


class TableTempLogParser(BaseParser):
    name = "table_temp_log"
    version = "0.1.0"
    file_family = SourceFileFamily.table_temp_log
    role = FileRole.secondary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        table, diagnostics, metadata = parse_table_stream(path, max_rows=5000)
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            tables=[table],
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if table.malformed_rows else ["ok"],
            metadata=metadata | {"streaming": True},
        )

