import re
from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import TIMESTAMP_PATTERNS, date_hint_from_filename, parse_timestamp_token


CODE_VALUE_RE = re.compile(r"(?P<code>[A-Za-zА-Яа-я_]*\d{1,4})\s*[:=]\s*(?P<value>[-+.\wА-Яа-я]+)")


def split_embedded_timestamp_entries(line: str) -> list[str]:
    for pattern in TIMESTAMP_PATTERNS:
        matches = list(pattern.finditer(line))
        if len(matches) > 1:
            entries = [
                line[m.start(): (matches[i + 1].start() if i + 1 < len(matches) else len(line))].strip()
                for i, m in enumerate(matches)
            ]
            return [e for e in entries if e]
    return [line]


class Monitor100LogParser(BaseParser):
    name = "monitor100_log"
    version = "0.1.0"
    file_family = SourceFileFamily.monitor100_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        glued_count = 0

        for line_no, offset, line in iter_text_lines(path, encoding):
            entries = split_embedded_timestamp_entries(line)
            if len(entries) > 1:
                glued_count += len(entries) - 1
            for entry in entries:
                ts, raw_ts, uncertainty = parse_timestamp_token(entry, date_hint)
                search_area = entry.replace(raw_ts, "", 1) if raw_ts else entry
                code_match = CODE_VALUE_RE.search(search_area)
                payload = {"raw_text": entry}
                event_type = "monitor_transition"
                if code_match:
                    payload["code"] = code_match.group("code")
                    payload["value"] = code_match.group("value")
                    event_type = f"monitor_code:{code_match.group('code')}"
                events.append(
                    CanonicalEventDraft(
                        ts=ts,
                        raw_timestamp=raw_ts,
                        ts_uncertainty=uncertainty,
                        source=SourceLocation(
                            source_file_id=context.source_file_id,
                            source_line=line_no,
                            source_offset=offset,
                            raw_excerpt=entry[:500],
                        ),
                        subsystem="monitor100",
                        event_type=event_type,
                        payload=payload,
                        confidence=0.9 if ts else 0.65,
                    )
                )
        if glued_count:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="glued_monitor_entries_recovered",
                    message="Recovered embedded Monitor100 entries by scanning timestamps inside lines.",
                    context={"extra_entries": glued_count},
                )
            )
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if glued_count else ["ok"],
            metadata={"encoding": encoding, "entry_count": len(events), "glued_entries_recovered": glued_count},
        )


class Monitor200LogParser(Monitor100LogParser):
    name = "monitor200_log"
    file_family = SourceFileFamily.monitor200_log
    role = FileRole.auxiliary
