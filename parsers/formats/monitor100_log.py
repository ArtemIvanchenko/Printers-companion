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
# A damaged timestamp occasionally leaves one or two characters before the real
# record marker (for example ``011:03:17 |S|...`` -> ``0 |S|...`` after the
# timestamp token is removed).  Only use this recovery when a timestamp was
# actually parsed; otherwise numeric continuation fragments could become records.
_PREFIXED_ENTRY_TYPE_RE = re.compile(
    r"^\s*([^|\r\n]{1,8}?)\s*\|([A-Za-zА-Яа-я]|\d)\|(.*)$",
    re.DOTALL,
)
_TRUNCATED_ENTRY_TYPE_RE = re.compile(r"^\s*\|([A-Za-zА-Яа-я]|\d)\s*$")
_PREFIXED_TRUNCATED_ENTRY_TYPE_RE = re.compile(
    r"^\s*([^|\r\n]{1,8}?)\s*\|([A-Za-zА-Яа-я]|\d)\s*$"
)
_STANDALONE_MARKER_RE = re.compile(r"^\s*(X)\s*$")

# Minimum widths observed for every stable firmware record layout in the real
# M-450M corpus.  Some types have multiple *valid* wider variants, so this is a
# lower-bound check rather than an exact schema assertion.
_MIN_FIELDS_BY_TYPE = {
    "S": 3,
    "R": 10,
    "P": 30,
    "T": 1,
    "F": 3,
    "M": 1,
    "D": 2,
    "1": 24,
    "3": 2,
    "7": 4,
}

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


def _record_match(
    entry: str,
    raw_ts: str,
) -> tuple[re.Match[str] | None, str | None, bool]:
    """Return a record match, any recovered prefix, and a truncated flag."""
    search_area = entry.replace(raw_ts, "", 1) if raw_ts else entry
    if match := _ENTRY_TYPE_RE.search(search_area):
        return match, None, False
    if raw_ts and (match := _PREFIXED_ENTRY_TYPE_RE.search(search_area)):
        return match, match.group(1).strip(), False
    if match := _TRUNCATED_ENTRY_TYPE_RE.search(search_area):
        return match, None, True
    if raw_ts and (match := _PREFIXED_TRUNCATED_ENTRY_TYPE_RE.search(search_area)):
        return match, match.group(1).strip(), True
    return None, None, False


def _classify_entry(entry: str, raw_ts: str) -> tuple[str, dict]:
    """Return (event_type, payload) from a Monitor entry (timestamp already parsed)."""
    m, recovered_prefix, truncated_marker = _record_match(entry, raw_ts)
    if not m:
        search_area = entry.replace(raw_ts, "", 1) if raw_ts else entry
        if marker := _STANDALONE_MARKER_RE.fullmatch(search_area):
            return "monitor_marker", {
                "raw_text": entry,
                "marker": marker.group(1),
                "field_count": 0,
                "structurally_complete": True,
            }
        return "monitor_transition", {"raw_text": entry}

    type_group = 2 if recovered_prefix is not None else 1
    values_group = type_group + 1
    type_code = m.group(type_group)
    raw_values = "" if truncated_marker else m.group(values_group).rstrip("|")
    values = [] if not raw_values else [coerce_value(v) for v in raw_values.split("|")]
    minimum_fields = _MIN_FIELDS_BY_TYPE.get(type_code)
    structurally_complete = minimum_fields is None or len(values) >= minimum_fields
    base_payload: dict = {
        "raw_text": entry,
        "record_type": type_code,
        "field_count": len(values),
        "structurally_complete": structurally_complete,
    }
    if minimum_fields is not None:
        base_payload["expected_min_fields"] = minimum_fields
    if recovered_prefix:
        base_payload["recovered_prefix"] = recovered_prefix

    if type_code == "R":
        # Realtime reading: multiple firmware layouts carry 10+ sensor values.
        return "monitor_reading", {
            **base_payload,
            "values": values,
        }

    if type_code == "S":
        # State change: signal_id | value | signal_name
        payload = dict(base_payload)
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
            **base_payload,
            "values": values,
        }

    if type_code == "T":
        # Track/trajectory parameters
        return "monitor_track_params", {
            **base_payload,
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
        **base_payload,
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
    version = "0.4.0"
    file_family = SourceFileFamily.monitor100_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        glued_count = 0
        unstructured_count = 0
        truncated_count = 0
        recovered_prefix_count = 0
        marker_count = 0
        record_type_counts: dict[str, int] = {}
        truncated_record_type_counts: dict[str, int] = {}
        timeline = TimestampQualityTracker()

        for line_no, offset, line in iter_text_lines(path, encoding):
            entries = split_embedded_timestamp_entries(line)
            if len(entries) > 1:
                glued_count += len(entries) - 1

            for entry in entries:
                ts, raw_ts, uncertainty = parse_timestamp_token(entry, date_hint)
                ts = timeline.add(ts)
                # Try firmware record and marker formats first, then legacy CODE=value.
                event_type, payload = _classify_entry(entry, raw_ts or "")
                if event_type == "monitor_transition":
                    event_type, payload = _classify_entry_legacy(entry, raw_ts or "")
                    if event_type == "monitor_transition":
                        unstructured_count += 1
                record_type = payload.get("record_type")
                if isinstance(record_type, str):
                    record_type_counts[record_type] = record_type_counts.get(record_type, 0) + 1
                    if payload.get("structurally_complete") is False:
                        truncated_count += 1
                        truncated_record_type_counts[record_type] = (
                            truncated_record_type_counts.get(record_type, 0) + 1
                        )
                if payload.get("recovered_prefix"):
                    recovered_prefix_count += 1
                if event_type == "monitor_marker":
                    marker_count += 1
                structured = "record_type" in payload or event_type.startswith("monitor_code:")
                if event_type == "monitor_marker":
                    confidence = 0.85 if ts else 0.45
                elif payload.get("structurally_complete") is False:
                    confidence = 0.35 if ts else 0.2
                elif structured:
                    confidence = 0.95 if ts else 0.55
                else:
                    confidence = 0.7 if ts else 0.3
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
                    confidence=confidence,
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
        if truncated_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="monitor_truncated_records",
                message=(
                    f"{truncated_count} Monitor records had fewer fields than the "
                    "minimum supported firmware layout."
                ),
                context={
                    "count": truncated_count,
                    "record_type_counts": truncated_record_type_counts,
                },
            ))
        if recovered_prefix_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="monitor_prefixed_records_recovered",
                message=(
                    f"Recovered {recovered_prefix_count} Monitor records after damaged "
                    "timestamp prefixes."
                ),
                context={"count": recovered_prefix_count},
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
                glued_count or timeline.missing_count or unstructured_count or truncated_count
            ) else ["ok"],
            metadata={
                "encoding": encoding,
                "entry_count": len(events),
                "glued_entries_recovered": glued_count,
                "event_type_counts": type_counts,
                "record_type_counts": record_type_counts,
                "truncated_records": truncated_count,
                "truncated_record_type_counts": truncated_record_type_counts,
                "prefixed_records_recovered": recovered_prefix_count,
                "marker_entries": marker_count,
                "unstructured_entries": unstructured_count,
                **timeline.metadata(),
            },
        )


class Monitor200LogParser(Monitor100LogParser):
    name = "monitor200_log"
    file_family = SourceFileFamily.monitor200_log
    role = FileRole.auxiliary
