from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timeline import TimestampQualityTracker
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token
from parsers.formats._tables import split_row


class ErrorLogParser(BaseParser):
    name = "error_log"
    version = "0.1.1"
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
        timeline = TimestampQualityTracker()
        header_rows = 0
        for line_no, offset, line in iter_text_lines(path, encoding):
            if not line.strip():
                continue
            # Only a delimited, entirely named table header is skipped. Arbitrary
            # text (including untimestamped errors) remains a low-trust record.
            columns = [value.strip().lower() for value in split_row(line)]
            if (
                any(delimiter in line for delimiter in ("|", ";", "\t", ","))
                and len(columns) == 2
                and columns[0] in {"time", "timestamp", "datetime"}
                and columns[1] == "error"
            ):
                header_rows += 1
                continue
            ts, raw_ts, uncertainty = parse_timestamp_token(line, date_hint)
            ts = timeline.add(ts, clock_only=uncertainty > 0)
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
        diagnostics: list[ParseDiagnosticRecord] = []
        if not events:
            diagnostics.append(ParseDiagnosticRecord(
                severity="info", code="empty_error_log_low_trust",
                message="Error log has no data entries; this does not imply absence of machine problems.",
            ))
        elif timeline.missing_count or timeline.out_of_order_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning", code="error_log_timestamp_quality",
                message="Error entries were retained with missing or out-of-order timestamps.",
                context=timeline.metadata(),
            ))
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            diagnostics=diagnostics,
            data_quality=(
                ["empty"] if not events else
                ["partial_recovery"] if timeline.missing_count or timeline.out_of_order_count else ["ok"]
            ),
            metadata={
                "encoding": encoding, "low_trust_source": True,
                "header_rows": header_rows, "row_count": len(events), **timeline.metadata(),
            },
        )

