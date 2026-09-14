"""Shared import DTOs and failure categories; no dependency on worker execution."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from analytics.prediction.timing_snapshot import PreparedLayerTimings
from domain.enums.common import ImportJobStatus
from operator_journal.notifications import NotificationMessage


class RawArchiveUnavailableError(RuntimeError):
    """The NAS could not durably accept the raw batch before analysis."""


class RetryableImportError(RuntimeError):
    """Transient infrastructure failure that must not publish partial Done."""


class LeaseCheckUnavailableError(RetryableImportError):
    """The NAS could not confirm the current fenced lease."""


class ImportPersistenceError(RetryableImportError):
    """A required normalized parse artifact could not be committed."""


class ImportJobRecord(BaseModel):
    import_job_id: str = Field(default_factory=lambda: f"import_{uuid4().hex}")
    owner_node_id: str
    print_record_id: str | None = None
    source_path: str
    source_name: str
    source_kind: str = "folder"
    status: ImportJobStatus = ImportJobStatus.detected
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmation_deadline: datetime | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    postponed_until: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    lease_generation: int = 0
    ignored_by: str | None = None
    ignored_at: datetime | None = None
    last_stability_check_at: datetime | None = None
    # Local file stability is bounded. Mandatory NAS archival uses this counter
    # only to calculate a bounded exponential delay and never discards a valid
    # local batch merely because the NAS stayed offline for a long time.
    stability_check_attempts: int = 0
    file_snapshot: dict[str, dict[str, Any]] = Field(default_factory=dict)
    checksum_manifest: dict[str, str] = Field(default_factory=dict)
    source_objects: dict[str, str] = Field(default_factory=dict)
    session_ids: list[str] = Field(default_factory=list)
    report_ids: list[str] = Field(default_factory=list)
    missing_context_questions: list[dict[str, Any]] = Field(default_factory=list)
    notification_log: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)


class ImportExecutionResult(BaseModel):
    job: ImportJobRecord
    notifications: list[NotificationMessage] = Field(default_factory=list)
    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reports: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Prepared locally, never serialized into ImportJob JSON. The worker
    # publishes these rows with the overview/report in one fenced transaction.
    layer_timings: dict[str, PreparedLayerTimings] = Field(default_factory=dict)
    previous_session_tokens: dict[str, str] = Field(default_factory=dict)
