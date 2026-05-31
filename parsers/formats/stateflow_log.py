from pathlib import Path
from typing import Any

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult, StateTransitionDraft
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token
from parsers.formats._tables import build_header, coerce_value, reconcile_row_width, split_row


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


def infer_subsystem(changed_columns: list[str]) -> str | None:
    for column in changed_columns:
        for prefix, subsystem in SUBSYSTEM_HINTS.items():
            if column.lower().startswith(prefix.lower()):
                return subsystem
    return "stateflow" if changed_columns else None


class StateFlowLogParser(BaseParser):
    name = "stateflow_log"
    version = "0.1.0"
    file_family = SourceFileFamily.stateflow_log
    role = FileRole.primary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        encoding = estimate_encoding(path)
        date_hint = date_hint_from_filename(path)
        header: list[str] | None = None
        previous_state: dict[str, Any] | None = None
        previous_ts = None
        previous_offset: int | None = None
        raw_sample: str | None = None
        transitions: list[StateTransitionDraft] = []
        diagnostics: list[ParseDiagnosticRecord] = []
        row_count = 0
        malformed = 0

        for line_no, offset, line in iter_text_lines(path, encoding):
            if not line.strip():
                continue
            values = split_row(line)
            if header is None:
                has_timestamp_column = any(
                    "time" in value.lower() or "date" in value.lower() for value in values
                )
                if has_timestamp_column:
                    # Named header row with timestamp column — skip it, use as column names.
                    header = build_header(values)
                    continue
                else:
                    # Headerless file — auto-generate column names and fall through to
                    # treat this line as the first data row.
                    header = [f"col_{index}" for index in range(len(values))]
            original_width = len(values)
            values, is_malformed = reconcile_row_width(values, len(header))
            if is_malformed:
                malformed += 1
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
            ts, _raw_ts, _uncertainty = parse_timestamp_token(line, date_hint)
            row = {column: coerce_value(value) for column, value in zip(header, values, strict=True)}
            state = {
                column: value
                for column, value in row.items()
                if "time" not in column.lower() and "date" not in column.lower()
            }
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
                    duration = max((ts - previous_ts).total_seconds(), 0.0)
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
                previous_state = state
                previous_ts = ts
                previous_offset = offset
                raw_sample = line[:500]

        compression_ratio = (len(transitions) / row_count) if row_count else 0.0
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
            data_quality=["partial_recovery"] if malformed else ["ok"],
            metadata={
                "encoding": encoding,
                "row_count": row_count,
                "transition_count": len(transitions),
                "compression_ratio": compression_ratio,
                "streaming": True,
                "malformed_rows": malformed,
            },
        )

