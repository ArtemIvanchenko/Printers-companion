import logging
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from core.config.settings import Settings, get_settings
from core.utils.files import sha256_file
from domain.enums.common import ImportJobStatus
from domain.services.ingestion import IngestionService
from domain.services.session_grouping import group_files_into_sessions
from operator_journal.notifications import (
    NotificationMessage,
    build_copying_retry_message,
    build_import_confirmation_message,
    build_import_summary_message,
)
from parsers.base.registry import ParserRegistry
from profiles.base.profile import PrinterProfilePlugin
from reporting.json_report.generator import generate_session_json_report
from reporting.markdown_report.generator import generate_markdown_report
from storage.db.session import SessionLocal

logger = logging.getLogger(__name__)


class ImportJobRecord(BaseModel):
    import_job_id: str = Field(default_factory=lambda: f"import_{uuid4().hex}")
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
    ignored_by: str | None = None
    ignored_at: datetime | None = None
    last_stability_check_at: datetime | None = None
    stability_check_attempts: int = 0  # Track number of stability check retries
    file_snapshot: dict[str, dict[str, Any]] = Field(default_factory=dict)
    checksum_manifest: dict[str, str] = Field(default_factory=dict)
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


def detect_import_candidate(
    source_path: Path,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ImportExecutionResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    job = ImportJobRecord(
        source_path=str(source_path),
        source_name=source_path.name,
        source_kind="zip" if source_path.suffix.lower() == ".zip" else "folder",
        status=ImportJobStatus.detected,
        detected_at=now,
        updated_at=now,
        confirmation_deadline=now + timedelta(hours=settings.import_confirmation_timeout_hours),
    )
    job.audit_trail.append(_audit("detected", actor="watcher", at=now))
    notifications: list[NotificationMessage] = []
    if settings.require_operator_import_confirmation:
        job.status = ImportJobStatus.awaiting_operator_confirmation
        notifications.append(build_import_confirmation_message(job.import_job_id, job.source_name))
        job.audit_trail.append(_audit("await_operator_confirmation", actor="watcher", at=now))
    return _result(job, notifications)


def ignore_import_job(job: ImportJobRecord, actor: str = "operator", now: datetime | None = None) -> ImportExecutionResult:
    now = now or datetime.now(timezone.utc)
    job.status = ImportJobStatus.ignored
    job.ignored_by = actor
    job.ignored_at = now
    job.updated_at = now
    job.audit_trail.append(_audit("ignored", actor=actor, at=now))
    return _result(job, [])


def postpone_import_job(
    job: ImportJobRecord,
    retry_seconds: int | None = None,
    actor: str = "operator",
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ImportExecutionResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    retry_seconds = retry_seconds or settings.file_stability_retry_seconds
    job.status = ImportJobStatus.postponed
    job.postponed_until = now + timedelta(seconds=retry_seconds)
    job.updated_at = now
    job.audit_trail.append(_audit("postponed", actor=actor, at=now, details={"retry_seconds": retry_seconds}))
    return _result(job, [])


def confirm_import_job(
    job: ImportJobRecord,
    registry: ParserRegistry,
    profile: PrinterProfilePlugin | None = None,
    actor: str = "operator",
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ImportExecutionResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    job.confirmed_by = job.confirmed_by or actor
    job.confirmed_at = job.confirmed_at or now
    job.status = ImportJobStatus.checking_stability
    job.updated_at = now
    job.audit_trail.append(_audit("confirm_requested", actor=actor, at=now))

    source_path = Path(job.source_path)
    stability = check_source_stability(job, source_path, settings=settings, now=now)
    
    if not stability.stable:
        # Check if we've exceeded max retries
        if job.stability_check_attempts >= settings.file_stability_max_retries:
            job.status = ImportJobStatus.failed
            job.error = f"File stability check failed after {job.stability_check_attempts} attempts. Reason: {stability.reason}"
            job.updated_at = now
            job.audit_trail.append(
                _audit(
                    "stability_check_failed_max_retries",
                    actor="system",
                    at=now,
                    details={"reason": stability.reason, "attempts": job.stability_check_attempts},
                )
            )
            logger.error("Import job %s failed: max stability check retries exceeded", job.import_job_id)
            return _result(job, [])
        
        # Increment attempt counter and postpone
        job.stability_check_attempts += 1
        retry = settings.file_stability_retry_seconds
        job.status = ImportJobStatus.postponed
        job.postponed_until = now + timedelta(seconds=retry)
        job.updated_at = now
        job.audit_trail.append(
            _audit(
                "stability_check_deferred",
                actor="system",
                at=now,
                details={
                    "reason": stability.reason,
                    "retry_seconds": retry,
                    "attempt": job.stability_check_attempts,
                    "max_retries": settings.file_stability_max_retries,
                },
            )
        )
        return _result(job, [build_copying_retry_message(job.import_job_id, retry)])

    try:
        return execute_confirmed_import(job, registry=registry, profile=profile, settings=settings, now=now)
    except Exception as exc:  # pragma: no cover - defensive containment for worker/API paths
        job.status = ImportJobStatus.failed
        job.error = str(exc)
        job.updated_at = now
        job.audit_trail.append(_audit("failed", actor="system", at=now, details={"error": str(exc)}))
        logger.exception("Import job %s failed with exception", job.import_job_id)
        return _result(job, [])


def mark_import_job_confirmed(
    job: ImportJobRecord,
    actor: str = "operator",
    now: datetime | None = None,
) -> ImportExecutionResult:
    """Record operator confirmation without reading raw files.

    API and agent endpoints use this to preserve the security boundary: only the
    worker has the read-only raw-log mount and performs stability/import work.
    """
    now = now or datetime.now(timezone.utc)
    job.confirmed_by = actor
    job.confirmed_at = now
    job.status = ImportJobStatus.checking_stability
    job.updated_at = now
    job.audit_trail.append(_audit("confirm_requested", actor=actor, at=now))
    job.audit_trail.append(_audit("queued_for_worker", actor="system", at=now))
    return _result(job, [])


def retry_import_job(
    job: ImportJobRecord,
    registry: ParserRegistry,
    profile: PrinterProfilePlugin | None = None,
    actor: str = "operator",
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ImportExecutionResult:
    return confirm_import_job(job, registry=registry, profile=profile, actor=actor, settings=settings, now=now)


class StabilityResult(BaseModel):
    stable: bool
    reason: str
    snapshot: dict[str, dict[str, Any]]


def check_source_stability(
    job: ImportJobRecord,
    source_path: Path,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> StabilityResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    snapshot = snapshot_source(source_path)
    job.last_stability_check_at = now
    previous = job.file_snapshot
    job.file_snapshot = snapshot
    if not snapshot:
        return StabilityResult(stable=False, reason="source_missing_or_empty", snapshot=snapshot)
    youngest_age = min(now.timestamp() - item["mtime"] for item in snapshot.values())
    if youngest_age < settings.file_stability_seconds:
        return StabilityResult(stable=False, reason="files_too_recent", snapshot=snapshot)
    if previous and previous != snapshot:
        return StabilityResult(stable=False, reason="file_snapshot_changed", snapshot=snapshot)
    return StabilityResult(stable=True, reason="stable", snapshot=snapshot)


def snapshot_source(source_path: Path) -> dict[str, dict[str, Any]]:
    if not source_path.exists():
        return {}
    paths = [source_path] if source_path.is_file() else [path for path in source_path.rglob("*") if path.is_file()]
    snapshot: dict[str, dict[str, Any]] = {}
    for path in sorted(paths):
        try:
            stat = path.stat()
            with path.open("rb"):
                pass
        except OSError:
            continue
        key = str(path.relative_to(source_path.parent if source_path.is_file() else source_path))
        snapshot[key] = {"size": stat.st_size, "mtime": stat.st_mtime}
    return snapshot


def persist_parse_results_to_db(
    session_id: str,
    ingested_files: list[Any],
    now: datetime | None = None,
) -> tuple[int, int]:
    """Persist parse results (source files and canonical events) to database.
    
    Returns:
        (files_saved_count, events_saved_count)
    """
    from storage.repositories.runtime import RuntimeRepository
    
    now = now or datetime.now(timezone.utc)
    files_saved = 0
    events_saved = 0
    
    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        
        for ingested_file in ingested_files:
            # Save SourceFile
            source_file_id = f"file_{uuid4().hex}"
            file_name = Path(ingested_file.path).name
            try:
                repo.save_source_file(
                    source_file_id=source_file_id,
                    session_id=session_id,
                    file_name=file_name,
                    checksum=ingested_file.checksum,
                    original_path=ingested_file.path,
                    size_bytes=ingested_file.size_bytes,
                    family=ingested_file.classification.family,
                    role=ingested_file.classification.role,
                    encoding=ingested_file.encoding,
                    data_quality_status=ingested_file.data_quality_status,
                    metadata=ingested_file.metadata,
                )
                files_saved += 1
                logger.info("Saved source file %s for session %s", file_name, session_id)
            except Exception as exc:
                logger.error("Failed to save source file %s: %s", file_name, exc)
                continue
            
            # Save CanonicalEvents from parse_result (in batches)
            if ingested_file.parse_result and ingested_file.parse_result.events:
                batch: list[dict] = []
                for event_draft in ingested_file.parse_result.events:
                    # Source location fields live on the nested `source`
                    # (SourceLocation), not on the event draft itself.
                    source = getattr(event_draft, 'source', None)
                    batch.append({
                        "event_id": f"event_{uuid4().hex}",
                        "session_id": session_id,
                        "source_file_id": source_file_id,
                        "ts": getattr(event_draft, 'ts', None),
                        "raw_timestamp": getattr(event_draft, 'raw_timestamp', None),
                        "event_type": getattr(event_draft, 'event_type', 'unknown'),
                        "subsystem": getattr(event_draft, 'subsystem', None),
                        "phase": getattr(event_draft, 'phase', None),
                        "severity": getattr(event_draft, 'severity', 'info'),
                        "confidence": getattr(event_draft, 'confidence', 1.0),
                        "layer": getattr(event_draft, 'layer', None),
                        "source_line": getattr(source, 'source_line', None),
                        "source_offset": getattr(source, 'source_offset', None),
                        "raw_excerpt": getattr(source, 'raw_excerpt', None),
                        "payload": getattr(event_draft, 'payload', {}),
                        "evidence_kind": getattr(event_draft, 'evidence_kind', 'machine_log'),
                        "provenance": [{"source": "parser", "file": file_name}],
                    })
                    if len(batch) >= _EVENT_BATCH_SIZE:
                        events_saved += _flush_event_batch(repo, batch)
                        batch = []
                events_saved += _flush_event_batch(repo, batch)
        
        try:
            repo.commit()
        except Exception as exc:
            logger.error("Failed to commit parse results to database: %s", exc)
    
    return files_saved, events_saved


def create_analysis_jobs_for_session(session_id: str, now: datetime | None = None) -> int:
    """Create build/analysis jobs for a session after import completes.
    
    Returns:
        Number of jobs created
    """
    from domain.models.entities import BuildJob
    
    now = now or datetime.now(timezone.utc)
    jobs_created = 0
    
    with SessionLocal() as db:
        try:
            # Create a BuildJob for session analysis
            build_id = f"build_{uuid4().hex}"
            build_job = BuildJob(
                build_id=build_id,
                session_id=session_id,
                job_name=f"import_analysis_{session_id}",
                recipe=None,
                layer_count=None,
                payload={"status": "pending", "created_at": now.isoformat()},
            )
            db.add(build_job)
            db.commit()
            jobs_created = 1
            logger.info("Created build job %s for session %s", build_id, session_id)
        except Exception as exc:
            logger.error("Failed to create build job for session %s: %s", session_id, exc)
            try:
                db.rollback()
            except Exception:
                pass
    
    return jobs_created


def execute_confirmed_import(
    job: ImportJobRecord,
    registry: ParserRegistry,
    profile: PrinterProfilePlugin | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ImportExecutionResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    source_path = Path(job.source_path)
    work_root: Path
    cleanup: tempfile.TemporaryDirectory[str] | None = None
    if job.source_kind == "zip":
        cleanup = tempfile.TemporaryDirectory(prefix="printer-log-import-")
        work_root = Path(cleanup.name)
        with zipfile.ZipFile(source_path) as archive:
            safe_extract_zip(archive, work_root)
    else:
        work_root = source_path

    try:
        job.status = ImportJobStatus.importing
        job.updated_at = now
        job.checksum_manifest = calculate_checksum_manifest(work_root)
        job.audit_trail.append(_audit("checksums_calculated", actor="system", at=now, details={"file_count": len(job.checksum_manifest)}))

        ingest_result = IngestionService(registry, profile).parse(work_root)
        groups = group_files_into_sessions(ingest_result.files)
        sessions: dict[str, dict[str, Any]] = {}
        reports: dict[str, dict[str, Any]] = {}

        job.status = ImportJobStatus.analyzing
        job.updated_at = now
        # Lazy import: build_group_overview pulls the analytics stack.
        from domain.services.session_overview import build_group_overview
        for group in groups:
            # Use the deterministic group id so this (watcher/confirmation) path
            # converges with the startup/upload import paths — same print → same
            # session id → deduplicated, not a parallel duplicate.
            session_id = group.group_id

            # Create session record in DB first so FK constraints are satisfied
            _ensure_session_record(session_id, float(group.confidence) if group.confidence else 0.0)

            # Persist parse results (source files and canonical events) to database
            files_saved, events_saved = persist_parse_results_to_db(session_id, group.files, now=now)
            logger.info(
                "Persisted parse results for session %s: %d files, %d events",
                session_id, files_saved, events_saved
            )

            # Create analysis jobs for this session
            analysis_jobs_created = create_analysis_jobs_for_session(session_id, now=now)
            logger.info("Created %d analysis jobs for session %s", analysis_jobs_created, session_id)

            # Enrich exactly like the startup/upload paths: features, telemetry,
            # health, classification, data_quality. Storing the bare group stub
            # (the old behaviour) made the dashboard show these sessions as
            # INCOMPLETE/empty — this was the root cause of "half the graphs
            # are empty" when the watcher import path was active.
            overview = build_group_overview(
                session_id, group.files,
                start_ts=group.start_ts, end_ts=group.end_ts,
                grouping_confidence=float(group.confidence) if group.confidence else 0.0,
            )
            stripped_files = [f.model_dump(mode="json", exclude={"parse_result"}) for f in group.files]
            sessions[session_id] = {"files": stripped_files, "group": overview}
            report = generate_session_json_report(session_id, group.files)
            report["markdown"] = generate_markdown_report(report)
            reports[report["report_id"]] = report
            job.session_ids.append(session_id)
            job.report_ids.append(report["report_id"])
            job.missing_context_questions.extend(build_missing_context_questions(session_id, report))

        job.status = ImportJobStatus.reporting
        job.updated_at = now
        final_status = ImportJobStatus.needs_operator_context if job.missing_context_questions else ImportJobStatus.done
        job.status = final_status
        job.updated_at = now
        job.audit_trail.append(
            _audit(
                "import_analyze_report_complete",
                actor="system",
                at=job.updated_at,
                details={"sessions": job.session_ids, "reports": job.report_ids, "status": final_status.value},
            )
        )
        report_links = [f"/reports/{report_id}" for report_id in job.report_ids]
        notification = build_import_summary_message(
            job.import_job_id,
            final_status.value,
            report_links,
            job.missing_context_questions,
        )
        return _result(job, [notification], sessions=sessions, reports=reports)
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def calculate_checksum_manifest(root: Path) -> dict[str, str]:
    if root.is_file():
        return {root.name: sha256_file(root)}
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(root))] = sha256_file(path)
    return manifest


def safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for member in archive.infolist():
        destination = (root / member.filename).resolve()
        if not str(destination).startswith(str(root)):
            raise ValueError(f"Unsafe ZIP member path: {member.filename}")
    archive.extractall(root)


def build_missing_context_questions(session_id: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    features = report.get("session_summary", {}).get("features", {})
    required = {
        "material": "Подтвердите материал для сессии",
        "powder_batch": "Подтвердите партию порошка для сессии",
        "gas_cylinder_id": "Подтвердите баллон газа для сессии",
    }
    return [
        {
            "session_id": session_id,
            "field": field,
            "question": f"{prefix} {session_id}.",
        }
        for field, prefix in required.items()
        if not features.get(field)
    ]


def _result(
    job: ImportJobRecord,
    notifications: list[NotificationMessage],
    sessions: dict[str, dict[str, Any]] | None = None,
    reports: dict[str, dict[str, Any]] | None = None,
) -> ImportExecutionResult:
    for notification in notifications:
        job.notification_log.append(notification.model_dump(mode="json"))
    return ImportExecutionResult(
        job=job,
        notifications=notifications,
        sessions=sessions or {},
        reports=reports or {},
    )


_EVENT_BATCH_SIZE = 500


def _flush_event_batch(repo, batch: list[dict]) -> int:
    """Save and commit a batch of canonical events; returns how many were flushed."""
    if not batch:
        return 0
    for evt in batch:
        try:
            repo.save_canonical_event(**evt)
        except Exception as exc:
            logger.error("Failed to save event: %s", exc)
    try:
        repo.commit()
    except Exception as exc:
        logger.error("Failed to commit event batch: %s", exc)
    return len(batch)


def _ensure_session_record(session_id: str, grouping_confidence: float) -> None:
    """Create the BuildSession row up-front so later FK inserts are satisfied."""
    from domain.models.entities import BuildSession
    with SessionLocal() as db:
        if db.get(BuildSession, session_id) is None:
            db.add(BuildSession(
                session_id=session_id,
                status="import_processing",
                classification="INCOMPLETE_OR_UNKNOWN",
                classification_confidence=0.0,
                grouping_confidence=grouping_confidence,
            ))
            db.commit()
            logger.info("Created session record %s in database", session_id)


def _audit(action: str, actor: str, at: datetime, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": action, "actor": actor, "timestamp": at.isoformat(), "details": details or {}}
