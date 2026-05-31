import re
from datetime import timedelta
from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token


LAYER_RE = re.compile(r"(?:layer|слой)\s*[:=#-]?\s*(\d+)", re.IGNORECASE)
VERTICAL_RE = re.compile(r"(?:LIR|vertical|z|позици[яи])\s*[:= ]\s*([-+]?\d+(?:[.,]\d+)?)", re.IGNORECASE)


def classify_event_type(text: str) -> tuple[str, str | None]:
    lower = text.lower()
    if any(token in lower for token in ("pause", "пауза", "останов")):
        return "pause", "pause"
    if any(token in lower for token in ("resume", "продолж", "возобнов")):
        return "resume", "restart_attempts"
    if any(token in lower for token in ("restart", "перезапуск", "рестарт")):
        return "restart_attempt", "restart_attempts"
    if any(token in lower for token in ("finish", "end", "заверш", "конец")):
        return "finish", "finish"
    if any(token in lower for token in ("burn", "спек", "плав")):
        return "burn_event", "burn"
    if any(token in lower for token in ("start", "старт", "начал")):
        return "start", "init"
    if LAYER_RE.search(text):
        return "layer_reference", None
    return "log_message", None


class EventLogParser(BaseParser):
    name = "main_event_log"
    version = "0.1.0"
    file_family = SourceFileFamily.main_event_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        missing_timestamp_count = 0
        # Midnight-rollover state tracked inline to avoid a second pass over all events.
        day_shift = 0
        prev_ts = None

        for line_no, offset, line in iter_text_lines(path, encoding):
            if not line.strip():
                continue
            ts, raw_ts, uncertainty = parse_timestamp_token(line, date_hint)
            if ts is not None:
                candidate = ts + timedelta(days=day_shift)
                if prev_ts is not None and candidate + timedelta(hours=12) < prev_ts:
                    day_shift += 1
                    candidate = ts + timedelta(days=day_shift)
                ts = candidate
                prev_ts = ts
            layer_match = LAYER_RE.search(line)
            vertical_match = VERTICAL_RE.search(line)
            event_type, phase = classify_event_type(line)
            payload = {"raw_text": line}
            if vertical_match:
                payload["vertical_position"] = float(vertical_match.group(1).replace(",", "."))
            if ts is None:
                missing_timestamp_count += 1
            events.append(
                CanonicalEventDraft(
                    ts=ts,
                    raw_timestamp=raw_ts,
                    ts_uncertainty=uncertainty,
                    layer=int(layer_match.group(1)) if layer_match else None,
                    source=SourceLocation(
                        source_file_id=context.source_file_id,
                        source_line=line_no,
                        source_offset=offset,
                        raw_excerpt=line[:500],
                    ),
                    subsystem="operator_panel",
                    phase=phase,
                    event_type=event_type,
                    confidence=0.75 if ts is None else 0.95,
                    payload=payload,
                )
            )

        if missing_timestamp_count:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="missing_timestamps",
                    message=f"{missing_timestamp_count} event-log rows had no parseable timestamp.",
                    context={"count": missing_timestamp_count},
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
            data_quality=["partial_recovery"] if missing_timestamp_count else ["ok"],
            metadata={"encoding": encoding, "line_count": len(events)},
        )

