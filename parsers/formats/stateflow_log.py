from pathlib import Path
from typing import Any

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult, StateTransitionDraft
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timeline import TimestampQualityTracker
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token
from parsers.formats._tables import build_header, coerce_value, reconcile_row_width, split_row, table_delimiter


SUBSYSTEM_HINTS = {
    "State": "automation",
    "MotorMove": "motion",
    "Failure": "failure",
    "LockLaser": "laser_interlock",
    "FillBunker": "powder",
    "CheckParameters": "parameter_check",
    "Heating": "heating",
    "Voltage": "power",
    "Chamber": "chamber",
    "ChamberGlove": "glove",
    "ChamberDoor": "door",
}

TIMESTAMP_COLUMNS = {"time", "date", "timestamp", "datetime"}


def infer_subsystem(changed_columns: list[str]) -> str | None:
    for column in changed_columns:
        for prefix, subsystem in SUBSYSTEM_HINTS.items():
            if column.lower().startswith(prefix.lower()):
                return subsystem
    return "stateflow" if changed_columns else None


class StateFlowLogParser(BaseParser):
    name = "stateflow_log"
    version = "0.2.3"
    file_family = SourceFileFamily.stateflow_log
    role = FileRole.primary

    # stateFlow classification evidence is *supportive*, not required:
    # real_print is confirmed by burn_log_layers / main_event_print_start.
    # A 523 MB stateFlow file (2.6 M identical rows) is not worth reading —
    # it causes OOM and takes minutes.  Skip parsing above this threshold.
    MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024   # 10 MB
    # Secondary guard behind the size gate: a sub-10 MB file of tiny rows could
    # still hold hundreds of thousands of near-identical lines.  Stop scanning
    # once this many rows have been read (transitions are capped separately).
    MAX_ROWS = 500_000
    MAX_TRANSITIONS = 50_000

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        file_size = path.stat().st_size
        if file_size == 0:
            return self.empty_result(path, context)
        if file_size > self.MAX_FILE_SIZE_BYTES:
            return ParseResult(
                parser_name=self.name,
                parser_version=self.version,
                profile_id=context.profile_id,
                file_family=self.file_family,
                role=self.role,
                transitions=[],
                diagnostics=[
                    ParseDiagnosticRecord(
                        severity="info",
                        code="stateflow_skipped_large_file",
                        message=(
                            f"stateFlow file skipped: {file_size / 1024 / 1024:.1f} MB "
                            f"> {self.MAX_FILE_SIZE_BYTES // 1024 // 1024} MB limit. "
                            f"Classification uses burn_log and monitor100 evidence instead."
                        ),
                        context={"file_size_bytes": file_size,
                                 "limit_bytes": self.MAX_FILE_SIZE_BYTES},
                    )
                ],
                data_quality=["skipped_large_file"],
                metadata={"file_size_bytes": file_size, "skipped": True},
            )

        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        header: list[str] | None = None
        delimiter: str | None = None
        header_signature: list[str] | None = None
        header_rows = 0
        date_columns: list[int] = []
        time_columns: list[int] = []
        timestamp_columns: list[int] = []
        timestamp_column_names: set[str] = set()
        timeline = TimestampQualityTracker()
        previous_state: dict[str, Any] | None = None
        previous_state_values: list[str] | None = None
        reusable_state = False
        previous_ts = None
        previous_offset: int | None = None
        raw_sample: str | None = None
        transitions: list[StateTransitionDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        row_count = 0
        malformed = 0
        truncated = False

        for line_no, offset, line in iter_text_lines(path, encoding):
            if not line.strip():
                continue
            if delimiter is None:
                delimiter = table_delimiter(line)
            values = split_row(line, delimiter)
            # Stop early: the rest of the file is almost certainly identical rows.
            if row_count >= self.MAX_ROWS:
                diagnostics.append(
                    ParseDiagnosticRecord(
                        severity="info",
                        code="stateflow_rows_capped",
                        message=(
                            f"stateFlow parsing stopped at {self.MAX_ROWS:,} rows "
                            f"({len(transitions)} transitions found). "
                            f"File is likely much larger but contains identical repeated rows."
                        ),
                        source_line=line_no,
                        context={"max_rows": self.MAX_ROWS, "transitions_found": len(transitions)},
                    )
                )
                truncated = True
                break
            if header is None:
                normalized_columns = [value.strip().lower() for value in values]
                has_timestamp_column = any(value in TIMESTAMP_COLUMNS for value in normalized_columns)
                if has_timestamp_column:
                    # Named header row with timestamp column — skip it, use as column names.
                    header = build_header(values)
                    header_signature = [value.strip() for value in values]
                    header_rows += 1
                    date_columns = [i for i, value in enumerate(normalized_columns) if value == "date"]
                    time_columns = [
                        i for i, value in enumerate(normalized_columns)
                        if value in TIMESTAMP_COLUMNS and value != "date"
                    ]
                    timestamp_columns = date_columns + time_columns
                    timestamp_column_names = {header[index] for index in timestamp_columns}
                    continue
                else:
                    # Headerless file — auto-generate column names and fall through to
                    # treat this line as the first data row.
                    header = [f"col_{index}" for index in range(len(values))]
            if header_signature is not None and [value.strip() for value in values] == header_signature:
                header_rows += 1
                continue
            original_width = len(values)
            values, is_malformed = reconcile_row_width(values, len(header))
            if is_malformed:
                malformed += 1
                if malformed <= 20:
                    diagnostics.append(
                        ParseDiagnosticRecord(
                            severity="warning",
                            code="malformed_stateflow_row",
                            message=f"Expected {len(header)} stateFlow columns, found {original_width}.",
                            source_line=line_no,
                            source_offset=offset,
                            context={"raw": line[:300]},
                        )
                    )
            row_count += 1
            timestamp_text = (
                " ".join(values[index].strip() for index in timestamp_columns)
                if timestamp_columns else line
            )
            # A named Date column is explicit input even when empty or corrupt.
            # Its absence cannot be repaired by the filename's calendar hint.
            ts, _raw_ts, uncertainty = parse_timestamp_token(
                timestamp_text, None if date_columns else date_hint,
            )
            ts = timeline.add(ts, clock_only=uncertainty > 0)
            if ts is None and timeline.missing_count <= 20:
                diagnostics.append(ParseDiagnosticRecord(
                    severity="warning", code="stateflow_missing_timestamp",
                    message="stateFlow row has no valid timestamp; retained as state evidence.",
                    source_line=line_no, source_offset=offset,
                    context={"raw": line[:500], "raw_timestamp": _raw_ts},
                ))
            state_values = [
                value for column, value in zip(header, values, strict=True)
                if column not in timestamp_column_names
            ]
            if reusable_state and state_values == previous_state_values:
                # Row/time diagnostics above still cover every observation.
                # RLE anchors stay at the first row of the unchanged state.
                continue
            row = {column: coerce_value(value) for column, value in zip(header, values, strict=True)}
            state = {
                column: value
                for column, value in row.items()
                if column not in timestamp_column_names
            }
            previous_state_values = state_values
            # NaN is not equal to itself: repeated raw cells must retain the
            # existing non-reflexive transition comparison in that case.
            reusable_state = all(value == value for value in state.values())
            if previous_state is None:
                previous_state = state
                previous_ts = ts
                previous_offset = offset
                raw_sample = line[:500]
                continue
            changed = [
                column for column, value in state.items() if previous_state.get(column) != value
            ]
            if changed:
                duration = None
                if previous_ts is not None and ts is not None:
                    delta = (ts - previous_ts).total_seconds()
                    duration = delta if delta >= 0 else None
                if not truncated:
                    transitions.append(
                        StateTransitionDraft(
                            ts_start=previous_ts,
                            ts_end=ts,
                            duration_sec=duration,
                            changed_columns=changed,
                            previous_state={column: previous_state.get(column) for column in changed},
                            new_state={column: state.get(column) for column in changed},
                            subsystem=infer_subsystem(changed),
                            source_file_id=context.source_file_id,
                            source_offset_start=previous_offset,
                            source_offset_end=offset,
                            raw_excerpt_sample=raw_sample,
                            parser_version=self.version,
                            profile_version=context.profile_version,
                        )
                    )
                    if len(transitions) >= self.MAX_TRANSITIONS:
                        truncated = True
                        diagnostics.append(
                            ParseDiagnosticRecord(
                                severity="info",
                                code="stateflow_transitions_capped",
                                message=(
                                    f"stateFlow parsing stopped at {self.MAX_TRANSITIONS:,} "
                                    f"transitions (row {row_count:,}). File may be larger; "
                                    f"remaining rows skipped to prevent OOM."
                                ),
                                source_line=line_no,
                                context={"max_transitions": self.MAX_TRANSITIONS},
                            )
                        )
                        break  # stop reading — no point scanning remaining 500 MB
                previous_state = state
                previous_ts = ts
                previous_offset = offset
                raw_sample = line[:500]

        if malformed > 20:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning", code="malformed_stateflow_rows_aggregated",
                message=f"Suppressed {malformed - 20} additional malformed stateFlow row diagnostics.",
                context={"total": malformed, "examples_retained": 20},
            ))
        compression_ratio = (len(transitions) / row_count) if row_count else 0.0
        if not row_count:
            diagnostics.append(ParseDiagnosticRecord(
                severity="info", code="stateflow_no_data_rows",
                message="stateFlow contains no data rows; preserved as empty evidence.",
            ))
        if timeline.missing_count > 20:
            diagnostics.append(ParseDiagnosticRecord(
                severity="warning", code="stateflow_missing_timestamps_aggregated",
                message="Additional stateFlow rows have no valid timestamp.",
                context={"missing_timestamps": timeline.missing_count, "examples_retained": 20},
            ))
        if row_count and not transitions:
            diagnostics.append(
                ParseDiagnosticRecord(
                    severity="info",
                    code="stateflow_no_transitions",
                    message="stateFlow rows were parsed but no state changes were detected.",
                    context={"row_count": row_count},
                )
            )
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            transitions=transitions,
            diagnostics=diagnostics,
            data_quality=(
                (["empty"] if not row_count else [])
                + (["partial_recovery"] if (
                    malformed or timeline.missing_count or timeline.out_of_order_count
                ) else [])
                + (["truncated"] if truncated else [])
            ) or ["ok"],
            metadata={
                "encoding": encoding,
                "row_count": row_count,
                "header_rows": header_rows,
                "repeated_headers": max(0, header_rows - 1),
                "transition_count": len(transitions),
                "compression_ratio": compression_ratio,
                "streaming": True,
                "malformed_rows": malformed,
                "truncated": truncated,
                **timeline.metadata(),
            },
        )
