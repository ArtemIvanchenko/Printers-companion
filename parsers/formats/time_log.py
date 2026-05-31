from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token
from parsers.formats._tables import parse_table_stream


CONCEPTS = {
    "pour_start": ("Pour_Start", "pour start", "засып"),
    "pour_end": ("Pour_End", "pour end"),
    "burn_start": ("Burn_Start", "burn start"),
    "burn_end": ("Burn_End", "burn end"),
    "layer_end": ("Layer_End", "layer end", "конец слоя"),
}


class TimeLogParser(BaseParser):
    name = "time_log"
    version = "0.1.0"
    file_family = SourceFileFamily.time_log
    role = FileRole.secondary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        table, diagnostics, metadata = parse_table_stream(path, max_rows=5000)
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        for line_no, offset, line in iter_text_lines(path, encoding):
            lower = line.lower()
            for event_type, aliases in CONCEPTS.items():
                if any(alias.lower() in lower for alias in aliases):
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
                            subsystem="layer_timing",
                            phase="burn" if "burn" in event_type else "pour",
                            event_type=event_type,
                            payload={"raw_text": line},
                            confidence=0.8,
                        )
                    )
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            tables=[table],
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if table.malformed_rows else ["ok"],
            metadata=metadata,
        )

