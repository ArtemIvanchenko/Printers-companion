"""Parser for Monitor100/200 logs.

Line format: HH:MM:SS |TYPE|val1|val2|...|

TYPE codes:
  |R|  — Realtime sensor reading (13 numeric pressure/flow values)
  |S|  — State change: |S|signal_id|value|signal_name|
  |P|  — Print parameters snapshot (laser power, speed, etc.)
  |T|  — Track/trajectory parameters

Some lines are "glued" (two entries concatenated without a newline between them).
split_embedded_timestamp_entries() recovers them by scanning for embedded timestamps.
"""
import re
from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timeline import TimestampQualityTracker
from parsers.common.timestamps import TIMESTAMP_PATTERNS, date_hint_from_filename, parse_timestamp_token
from parsers.formats._tables import coerce_value

# The firmware emits letter and numeric record types (R/S/P/T/F/M/D/1/7...).
# Anchor the marker at the start of the timestamp-stripped text so a numeric
# payload cell cannot be mistaken for a record type in a damaged continuation.
_ENTRY_TYPE_RE = re.compile(r"^\s*\|([A-Za-zА-Яа-я]|\d)\|(.*)$", re.DOTALL)

# Fallback: legacy "CODE=value" / "CODE:value" pairs seen in older firmware logs
_CODE_VALUE_RE = re.compile(r"(?P<code>[A-Za-zА-Яа-я_]*\d{1,4})\s*[:=]\s*(?P<value>[-+.\wА-Яа-я]+)")


def split_embedded_timestamp_entries(line: str) -> list[str]:
    """Recover multiple entries glued on a single line by scanning for timestamps."""
    for pattern in TIMESTAMP_PATTERNS:
        matches = list(pattern.finditer(line))
        if len(matches) > 1:
            entries = [
                line[m.start(): (matches[i + 1].start() if i + 1 < len(matches) else len(line))].strip()
                for i, m in enumerate(matches)
            ]
            return [e for e in entries if e]
    return [line]


def _classify_entry(entry: str, raw_ts: str) -> tuple[str, dict]:
    """Return (event_type, payload) from a Monitor entry (timestamp already parsed)."""
    search_area = entry.replace(raw_ts, "", 1) if raw_ts else entry
    m = _ENTRY_TYPE_RE.search(search_area)
    if not m:
        return "monitor_transition", {"raw_text": entry}

    type_code = m.group(1)
    # Strip trailing pipes and split
    values = [coerce_value(v) for v in m.group(2).rstrip("|").split("|")]

    if type_code == "R":
        # Realtime reading: 12-13 numeric pressure/flow values
        return "monitor_reading", {
            "raw_text": entry,
            "record_type": type_code,
            "values": values,
        }

    if type_code == "S":
        # State change: signal_id | value | signal_name
        payload: dict = {"raw_text": entry, "record_type": type_code}
        if len(values) >= 3:
            payload["signal_id"] = values[0]
            payload["signal_value"] = values[1]
            payload["signal_name"] = values[2]
        elif len(values) == 2:
            payload["signal_id"] = values[0]
            payload["signal_value"] = values[1]
        return "monitor_state_change", payload

    if type_code == "P":
        # Print parameters snapshot
        return "monitor_print_params", {
            "raw_text": entry,
            "record_type": type_code,
            "values": values,
        }

    if type_code == "T":
        # Track/trajectory parameters
        return "monitor_track_params", {
            "raw_text": entry,
            "record_type": type_code,
            "values": values,
        }

    semantic_types = {
        "F": "monitor_frequency_change",
        "M": "monitor_message",
        "D": "monitor_diagnostic",
        "1": "monitor_motion_snapshot",
        "7": "monitor_axis_snapshot",
    }
    return semantic_types.get(type_code, f"monitor_record:{type_code}"), {
        "raw_text": entry,
        "record_type": type_code,
        "values": values,
    }


def _classify_entry_legacy(entry: str, raw_ts: str) -> tuple[str, dict]:
    """Fallback for firmware logs that use CODE=value / CODE:value syntax."""
    search_area = entry.replace(raw_ts, "", 1) if raw_ts else entry
    m = _CODE_VALUE_RE.search(search_area)
    payload: dict = {"raw_text": entry}
    event_type = "monitor_transition"
    if m:
        payload["code"] = m.group("code")
        payload["value"] = m.group("value")
        event_type = f"monitor_code:{m.group('code')}"
    return event_type, payload


class Monitor100LogParser(BaseParser):
    name = "monitor100_log"
    version = "0.3.0"
    file_family = SourceFileFamily.monitor100_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        glued_count = 0
        unstructured_count = 0
        record_type_counts: dict[str, int] = {}
        timeline = TimestampQualityTracker()

        for line_no, offset, line in iter_text_lines(path, encoding):
            entries = split_embedded_timestamp_entries(line)
            if len(entries) > 1:
                glued_count += len(entries) - 1

            for entry in entries:
                ts, raw_ts, uncertainty = parse_timestamp_token(entry, date_hint)
                ts = timeline.add(ts)
                # Try |R|S|P|T| format first; fall back to legacy CODE=value
                search_area = entry.replace(raw_ts, "", 1) if raw_ts else entry
                if _ENTRY_TYPE_RE.search(search_area):
                    event_type, payload = _classify_entry(entry, raw_ts or "")
                    record_type = payload.get("record_type")
                    if isinstance(record_type, str):
                        record_type_counts[record_type] = record_type_counts.get(record_type, 0) + 1
                else:
                    event_type, payload = _classify_entry_legacy(entry, raw_ts or "")
                    if event_type == "monitor_transition":
                        unstructured_count += 1
                structured = "record_type" in payload or event_type.startswith("monitor_code:")
                events.append(CanonicalEventDraft(
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
                    confidence=(0.95 if ts else 0.55) if structured else (0.7 if ts else 0.3),
                ))

        if glued_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="glued_monitor_entries_recovered",
                message="Recovered embedded Monitor100 entries by scanning timestamps inside lines.",
                context={"extra_entries": glued_count},
            ))
        if timeline.missing_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="monitor_missing_timestamps",
                message=f"{timeline.missing_count} Monitor entries had no parseable timestamp.",
                context={"count": timeline.missing_count},
            ))
        if timeline.out_of_order_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="monitor_out_of_order_timestamps",
                message=f"{timeline.out_of_order_count} Monitor timestamps moved backwards.",
                context={"count": timeline.out_of_order_count},
            ))
        if unstructured_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="monitor_unstructured_entries",
                message=f"{unstructured_count} Monitor entries had no recognizable record marker.",
                context={"count": unstructured_count},
            ))

        type_counts: dict[str, int] = {}
        for e in events:
            type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1

        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if (
                glued_count or timeline.missing_count or unstructured_count
            ) else ["ok"],
            metadata={
                "encoding": encoding,
                "entry_count": len(events),
                "glued_entries_recovered": glued_count,
                "event_type_counts": type_counts,
                "record_type_counts": record_type_counts,
                "unstructured_entries": unstructured_count,
                **timeline.metadata(),
            },
        )


class Monitor200LogParser(Monitor100LogParser):
    name = "monitor200_log"
    file_family = SourceFileFamily.monitor200_log
    role = FileRole.auxiliary
