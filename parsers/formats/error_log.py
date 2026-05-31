from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token


class ErrorLogParser(BaseParser):
    name = "error_log"
    version = "0.1.0"
    file_family = SourceFileFamily.error_log
    role = FileRole.auxiliary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        if path.stat().st_size == 0:
            result = self.empty_result(path, context)
            result.diagnostics.append(
                ParseDiagnosticRecord(
                    severity="info",
                    code="empty_error_log_low_trust",
                    message="Empty error.log does not imply absence of machine problems.",
                )
            )
            return result
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        for line_no, offset, line in iter_text_lines(path, encoding):
            if not line.strip():
                continue
            ts, raw_ts, uncertainty = parse_timestamp_token(line, date_hint)
            events.append(
                CanonicalEventDraft(
                    ts=ts,
                    raw_timestamp=raw_ts,
                    ts_uncertainty=uncertainty,
                    source=SourceLocation(
                        source_file_id=context.source_file_id,
                        source_line=line_no,
                        source_offset=offset,
                        raw_excerpt=line[:500],
                    ),
                    subsystem="error_log",
                    event_type="low_trust_error_entry",
                    severity="warning",
                    confidence=0.5,
                    payload={"raw_text": line, "low_trust_source": True},
                )
            )
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            data_quality=["ok"],
            metadata={"encoding": encoding, "low_trust_source": True},
        )

