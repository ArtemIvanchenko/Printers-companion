"""Parser for *_time.log — per-layer timing statistics.

Format (alternating line pairs):
  OLD_STATS: N | pour_ms | burn_ms | make_layer_ms |
  NEW_STATS: LN_detailed | Key:abs_ms | Key:abs_ms | ...

OLD_STATS  — summary durations for layer N (milliseconds).
NEW_STATS  — absolute machine-clock millisecond timestamps for each sub-event.
             The epoch is internal (machine uptime), not UTC wall-clock, so
             ts=None; absolute values are kept in payload for cross-log correlation.

The first file line blends the column-name header with the first OLD_STATS entry:
  "  N|  poor|  burn|make layer|OLD_STATS:  2|  8829| 40218| 49250|"
Both OLD_STATS and NEW_STATS parsers handle this via regex.search (not match).
"""
import re
from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import CanonicalEventDraft, ParseDiagnosticRecord, ParseResult, SourceLocation
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines

# OLD_STATS: N | pour_ms | burn_ms | make_layer_ms
_OLD_RE = re.compile(
    r"OLD_STATS:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)"
)

# NEW_STATS: LN_detailed | Key:abs_ms | ...
_NEW_HDR_RE = re.compile(r"NEW_STATS:\s*L(\d+)_detailed\|(.+)")
_KV_RE = re.compile(r"(\w+):(\d+)")

# Maps NEW_STATS key names → canonical event_type
_KEY_TO_EVENT: dict[str, str] = {
    "Pour_Start":     "pour_start",
    "Pour_End":       "pour_end",
    "Burn_Start":     "burn_start",
    "Burn_End":       "burn_end",
    "Layer_End":      "layer_end",
    "MakeLayer_Start": "make_layer_start",
}


class TimeLogParser(BaseParser):
    name = "time_log"
    version = "0.2.0"
    file_family = SourceFileFamily.time_log
    role = FileRole.secondary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        malformed = 0

        for line_no, offset, line in iter_text_lines(path, encoding):
            stripped = line.strip()
            if not stripped:
                continue

            if "OLD_STATS:" in line:
                m = _OLD_RE.search(line)
                if not m:
                    malformed += 1
                    continue
                layer_n, pour_ms, burn_ms, make_ms = (int(x) for x in m.groups())
                events.append(CanonicalEventDraft(
                    ts=None,
                    layer=layer_n,
                    source=SourceLocation(
                        source_file_id=context.source_file_id,
                        source_line=line_no,
                        source_offset=offset,
                        raw_excerpt=line[:300],
                    ),
                    subsystem="layer_timing",
                    phase="layer",
                    event_type="layer_timing_summary",
                    payload={
                        "layer": layer_n,
                        "pour_ms": pour_ms,
                        "burn_ms": burn_ms,
                        "make_layer_ms": make_ms,
                    },
                    confidence=0.95,
                ))

            elif "NEW_STATS:" in line:
                m = _NEW_HDR_RE.search(line)
                if not m:
                    malformed += 1
                    continue
                layer_n = int(m.group(1))
                kv_part = m.group(2)
                pairs = _KV_RE.findall(kv_part)
                if not pairs:
                    malformed += 1
                    continue
                for key, abs_ms_str in pairs:
                    event_type = _KEY_TO_EVENT.get(key, f"time_{key.lower()}")
                    phase = "burn" if "burn" in event_type else "pour" if "pour" in event_type else "layer"
                    events.append(CanonicalEventDraft(
                        ts=None,
                        layer=layer_n,
                        source=SourceLocation(
                            source_file_id=context.source_file_id,
                            source_line=line_no,
                            source_offset=offset,
                            raw_excerpt=line[:300],
                        ),
                        subsystem="layer_timing",
                        phase=phase,
                        event_type=event_type,
                        payload={
                            "layer": layer_n,
                            "abs_ms": int(abs_ms_str),
                            "key": key,
                        },
                        confidence=0.9,
                    ))

        if malformed:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning",
                code="time_log_malformed_rows",
                message=f"{malformed} time_log lines did not match OLD_STATS/NEW_STATS pattern.",
                context={"count": malformed},
            ))

        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            diagnostics=diagnostics,
            data_quality=["partial_recovery"] if malformed else ["ok"],
            metadata={"encoding": encoding, "event_count": len(events)},
        )
