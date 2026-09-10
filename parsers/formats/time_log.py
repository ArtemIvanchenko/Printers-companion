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
from collections import defaultdict
from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import (
    CanonicalEventDraft,
    ParseDiagnosticRecord,
    ParseResult,
    SourceLocation,
)
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines

# OLD_STATS: N | pour_ms | burn_ms | make_layer_ms
_OLD_RE = re.compile(r"OLD_STATS:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|?\s*$")

# NEW_STATS: LN_detailed | Key:abs_ms | ...
_NEW_HDR_RE = re.compile(r"NEW_STATS:\s*L(\d+)_detailed\|(.+)")
_KV_RE = re.compile(r"(\w+):\s*(\d+)")

# Maps NEW_STATS key names → canonical event_type
_KEY_TO_EVENT: dict[str, str] = {
    "Pour_Start": "pour_start",
    "Pour_End": "pour_end",
    "Burn_Start": "burn_start",
    "Burn_End": "burn_end",
    "Layer_End": "layer_end",
    "MakeLayer_Start": "make_layer_start",
}


class TimeLogParser(BaseParser):
    name = "time_log"
    version = "0.5.0"
    file_family = SourceFileFamily.time_log
    role = FileRole.secondary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        events: list[CanonicalEventDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        malformed = 0
        summaries: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        details: dict[int, list[dict[str, int]]] = defaultdict(list)
        paired: list[tuple[CanonicalEventDraft, dict[str, int]]] = []
        pending: CanonicalEventDraft | None = None

        for line_no, offset, line in iter_text_lines(path, encoding):
            stripped = line.strip()
            if not stripped:
                continue

            if "OLD_STATS:" in line:
                pending = None
                m = _OLD_RE.search(line)
                if not m:
                    malformed += 1
                    continue
                layer_n, pour_ms, burn_ms, make_ms = (int(x) for x in m.groups())
                summaries[layer_n].append((pour_ms, burn_ms, make_ms))
                events.append(
                    CanonicalEventDraft(
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
                    )
                )
                pending = events[-1]
                components = pour_ms + burn_ms
                pending.payload["timing_valid"] = (
                    layer_n > 0 and make_ms + max(20, components * 0.01) >= components
                )
                pending.payload["timing_validation"] = "summary_only"

            elif "NEW_STATS:" in line:
                m = _NEW_HDR_RE.search(line)
                if not m:
                    malformed += 1
                    pending = None
                    continue
                layer_n = int(m.group(1))
                kv_part = m.group(2)
                fields = [field.strip() for field in kv_part.split("|") if field.strip()]
                matches = [_KV_RE.fullmatch(field) for field in fields]
                pairs = [match.groups() for match in matches if match is not None]
                if (
                    not pairs
                    or len(pairs) != len(fields)
                    or len({key for key, _ in pairs}) != len(pairs)
                ):
                    malformed += 1
                    pending = None
                    continue
                detail = {key: int(value) for key, value in pairs}
                details[layer_n].append(detail)
                # Pair the actual neighbouring records, not the nth rows for
                # a layer: missing details must not shift a retry's evidence.
                if pending is not None and pending.layer == layer_n:
                    paired.append((pending, detail))
                pending = None
                for key, abs_ms_str in pairs:
                    event_type = _KEY_TO_EVENT.get(key, f"time_{key.lower()}")
                    phase = (
                        "burn"
                        if "burn" in event_type
                        else "pour"
                        if "pour" in event_type
                        else "layer"
                    )
                    events.append(
                        CanonicalEventDraft(
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
                        )
                    )
            else:
                pending = None

        if malformed:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="time_log_malformed_rows",
                    message=f"{malformed} time_log lines did not match OLD_STATS/NEW_STATS pattern.",
                    context={"count": malformed},
                )
            )

        paired_layers = 0
        comparison_count = 0
        mismatch_counts = {"pour": 0, "burn": 0, "make_layer": 0}
        negative_durations = 0
        impossible_cycles: list[int] = []
        for layer, summary_rows in summaries.items():
            for pour_ms, burn_ms, make_ms in summary_rows:
                components = pour_ms + burn_ms
                if make_ms + max(20, components * 0.01) < components:
                    impossible_cycles.append(layer)
        for summary_event, detail in paired:
            summary = summary_event.payload
            paired_layers += 1
            pour_ms, burn_ms, make_ms = (
                summary["pour_ms"],
                summary["burn_ms"],
                summary["make_layer_ms"],
            )
            comparisons = {
                "pour": (pour_ms, detail.get("Pour_Start"), detail.get("Pour_End")),
                "burn": (burn_ms, detail.get("Burn_Start"), detail.get("Burn_End")),
                "make_layer": (make_ms, detail.get("MakeLayer_Start"), detail.get("Layer_End")),
            }
            checked = mismatched = 0
            for name, (reported, started, ended) in comparisons.items():
                if started is None or ended is None:
                    continue
                comparison_count += 1
                checked += 1
                measured = ended - started
                if measured < 0:
                    negative_durations += 1
                    mismatch_counts[name] += 1
                    mismatched += 1
                elif abs(measured - reported) > max(20, reported * 0.01):
                    mismatch_counts[name] += 1
                    mismatched += 1
            summary["timing_validation"] = (
                "mismatch"
                if mismatched
                else "verified"
                if checked == 3
                else "partial"
                if checked
                else "summary_only"
            )
            if mismatched:
                summary["timing_valid"] = False
                summary_event.confidence = 0.3

        total_mismatches = sum(mismatch_counts.values())
        duplicate_summaries = sum(max(0, len(rows) - 1) for rows in summaries.values())
        equivalent_duplicates = conflicting_duplicates = 0
        conflicting_layers: list[int] = []
        for layer, rows in summaries.items():
            if len(rows) < 2:
                continue
            first = rows[0]
            for candidate in rows[1:]:
                materially_different = any(
                    abs(previous - current) > max(20, abs(previous) * 0.01)
                    for previous, current in zip(first, candidate)
                )
                if materially_different:
                    conflicting_duplicates += 1
                    conflicting_layers.append(layer)
                else:
                    equivalent_duplicates += 1
        if total_mismatches:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="time_log_duration_mismatch",
                    message=(
                        f"{total_mismatches} OLD_STATS durations disagreed with absolute "
                        "NEW_STATS markers beyond the numerical tolerance."
                    ),
                    context={
                        "paired_layers": paired_layers,
                        "mismatch_counts": mismatch_counts,
                        "negative_durations": negative_durations,
                    },
                )
            )
        if duplicate_summaries:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="info",
                    code="time_log_duplicate_layers",
                    message=(
                        f"Found {duplicate_summaries} repeated layer summary records: "
                        f"{equivalent_duplicates} equivalent boundary copies and "
                        f"{conflicting_duplicates} possible repeat attempts."
                    ),
                    context={
                        "count": duplicate_summaries,
                        "equivalent": equivalent_duplicates,
                        "conflicting": conflicting_duplicates,
                        "conflicting_layers": sorted(set(conflicting_layers))[:100],
                    },
                )
            )
        if impossible_cycles:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="warning",
                    code="time_log_impossible_cycle",
                    message=(
                        f"{len(impossible_cycles)} layer cycles are shorter than "
                        "burn_ms + pour_ms beyond numerical tolerance."
                    ),
                    context={
                        "count": len(impossible_cycles),
                        "layers": sorted(set(impossible_cycles))[:100],
                    },
                )
            )

        consistency_score = (
            100.0 * (1.0 - total_mismatches / comparison_count) if comparison_count else None
        )

        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            events=events,
            diagnostics=diagnostics,
            data_quality=["partial_recovery"]
            if (malformed or total_mismatches or impossible_cycles)
            else ["ok"],
            metadata={
                "encoding": encoding,
                "event_count": len(events),
                "summary_count": sum(len(rows) for rows in summaries.values()),
                "detailed_count": sum(len(rows) for rows in details.values()),
                "paired_layers": paired_layers,
                "unpaired_summaries": sum(len(rows) for rows in summaries.values()) - paired_layers,
                "unpaired_details": sum(len(rows) for rows in details.values()) - paired_layers,
                "duration_mismatch_counts": mismatch_counts,
                "duration_consistency_score": round(consistency_score, 4)
                if consistency_score is not None
                else None,
                "duplicate_layer_summaries": duplicate_summaries,
                "equivalent_layer_duplicates": equivalent_duplicates,
                "conflicting_layer_duplicates": conflicting_duplicates,
                "impossible_cycle_count": len(impossible_cycles),
            },
        )
