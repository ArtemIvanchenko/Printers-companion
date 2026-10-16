from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from domain.enums.common import DataQualityStatus, EvidenceKind, FileRole, SourceFileFamily


class SourceLocation(BaseModel):
    source_file_id: str | None = None
    source_line: int | None = None
    source_offset: int | None = None
    source_offset_end: int | None = None
    raw_excerpt: str | None = None


class ParseDiagnosticRecord(BaseModel):
    severity: str = "info"
    code: str
    message: str
    source_line: int | None = None
    source_offset: int | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class CanonicalEventDraft(BaseModel):
    ts: datetime | None = None
    raw_timestamp: str | None = None
    ts_uncertainty: float = 0.0
    layer: int | None = None
    source: SourceLocation = Field(default_factory=SourceLocation)
    subsystem: str | None = None
    phase: str | None = None
    event_type: str
    severity: str = "info"
    confidence: float = 1.0
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_kind: EvidenceKind = EvidenceKind.machine_log


class StateTransitionDraft(BaseModel):
    ts_start: datetime | None = None
    ts_end: datetime | None = None
    duration_sec: float | None = None
    changed_columns: list[str] = Field(default_factory=list)
    previous_state: dict[str, Any] = Field(default_factory=dict)
    new_state: dict[str, Any] = Field(default_factory=dict)
    subsystem: str | None = None
    source_file_id: str | None = None
    source_offset_start: int | None = None
    source_offset_end: int | None = None
    raw_excerpt_sample: str | None = None
    parser_version: str
    profile_version: str | None = None


class ParsedTableBatch(BaseModel):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    unknown_columns: list[str] = Field(default_factory=list)
    malformed_rows: int = 0
    repeated_headers: int = 0


class ParseResult(BaseModel):
    parser_name: str
    parser_version: str
    profile_id: str | None = None
    file_family: SourceFileFamily
    role: FileRole
    events: list[CanonicalEventDraft] = Field(default_factory=list)
    transitions: list[StateTransitionDraft] = Field(default_factory=list)
    tables: list[ParsedTableBatch] = Field(default_factory=list)
    diagnostics: list[ParseDiagnosticRecord] = Field(default_factory=list)
    data_quality: list[DataQualityStatus] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def malformed_row_count(self) -> int:
        """Count integer row facts, never bounded diagnostic examples.

        Tables are authoritative when present: their counters are commonly
        duplicated in metadata. Non-tabular parsers retain the count there.
        This property is deliberately not an additional serialized field.
        """
        if self.tables:
            return sum(table.malformed_rows for table in self.tables
                       if type(table.malformed_rows) is int and table.malformed_rows >= 0)
        count = self.metadata.get("malformed_rows", 0)
        return count if type(count) is int and count >= 0 else 0

    @property
    def repeated_header_count(self) -> int:
        """Use observed counters, with tables authoritative over metadata."""
        if self.tables:
            return sum(table.repeated_headers for table in self.tables
                       if type(table.repeated_headers) is int and table.repeated_headers >= 0)
        count = self.metadata.get("repeated_headers", 0)
        return count if type(count) is int and count >= 0 else 0


class FileClassification(BaseModel):
    path: str
    file_name: str
    family: SourceFileFamily
    role: FileRole
    confidence: float
    matched_pattern: str | None = None

