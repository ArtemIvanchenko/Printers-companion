import logging
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from core.config.settings import Settings, get_settings
from core.utils.files import iter_source_files, sha256_file
from domain.enums.common import ImportJobStatus
from domain.services.ingestion import IngestionService
from domain.services.importing.fence import ImportFence, StaleImportLeaseError
from analytics.prediction.timing_snapshot import PreparedLayerTimings
from domain.services.session_grouping import SessionGroup, group_files_into_sessions
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
# Compatibility exports keep existing API/CLI imports stable while the queue,
# persistence and calculation contracts no longer depend on each other's code.
from domain.services.importing.contracts import (
    ImportExecutionResult as ImportExecutionResult,
    ImportJobRecord as ImportJobRecord,
    ImportPersistenceError as ImportPersistenceError,
    LeaseCheckUnavailableError as LeaseCheckUnavailableError,
    RawArchiveUnavailableError as RawArchiveUnavailableError,
    RetryableImportError as RetryableImportError,
)
from domain.services.importing.persistence import (
    persist_parse_results_to_db as persist_parse_results_to_db,
    _ensure_session_record,
    _flush_event_batch as _flush_event_batch,
    _require_current_lease,
)

logger = logging.getLogger(__name__)


def detect_import_candidate(
    source_path: Path,
    settings: Settings | None = None,
    now: datetime | None = None,
    print_record_id: str | None = None,
    file_snapshot: dict[str, dict[str, Any]] | None = None,
) -> ImportExecutionResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    job = ImportJobRecord(
        owner_node_id=settings.compute_node_id,
        print_record_id=print_record_id,
        source_path=str(source_path),
        source_name=source_path.name,
        source_kind="zip" if source_path.suffix.lower() == ".zip" else "folder",
        status=ImportJobStatus.detected,
        detected_at=now,
        updated_at=now,
        confirmation_deadline=now + timedelta(hours=settings.import_confirmation_timeout_hours),
        # This first snapshot both identifies an unchanged ignored/completed
        # upload and removes an unnecessary extra stability-check cycle.
        file_snapshot=(
            file_snapshot if file_snapshot is not None else snapshot_source(source_path)
        ),
    )
    job.audit_trail.append(_audit("detected", actor="watcher", at=now))
    notifications: list[NotificationMessage] = []
    if settings.require_operator_import_confirmation:
        job.status = ImportJobStatus.awaiting_operator_confirmation
        notifications.append(
            build_import_confirmation_message(
                job.import_job_id,
                job.source_name,
                job.owner_node_id,
            )
        )
        job.audit_trail.append(_audit("await_operator_confirmation", actor="watcher", at=now))
    return _result(job, notifications)


def ignore_import_job(job: ImportJobRecord, actor: str = "operator", now: datetime | None = None) -> ImportExecutionResult:
    now = now or datetime.now(timezone.utc)
    job.status = ImportJobStatus.ignored
    job.ignored_by = actor
    job.ignored_at = now
    job.lease_owner = None
    job.lease_until = None
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
    job.lease_owner = None
    job.lease_until = None
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
    lease_guard: Callable[[], bool] | None = None,
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
        return _result(
            job,
            [build_copying_retry_message(job.import_job_id, retry, job.owner_node_id)],
        )

    try:
        return execute_confirmed_import(
            job,
            registry=registry,
            profile=profile,
            settings=settings,
            now=now,
            lease_guard=lease_guard,
        )
    except RawArchiveUnavailableError as exc:
        job.stability_check_attempts += 1
        job.error = str(exc)
        job.updated_at = now
        # A NAS outage is infrastructure downtime, not invalid input.  Keep the
        # owner-affine job and its local source retryable indefinitely; otherwise
        # a weekend outage would silently turn a perfectly valid log batch into
        # a terminal manual-recovery incident.
        retry = min(
            settings.nas_sync_retry_max_seconds,
            settings.nas_sync_retry_min_seconds
            * (2 ** min(job.stability_check_attempts - 1, 16)),
        )
        job.status = ImportJobStatus.postponed
        job.postponed_until = now + timedelta(seconds=retry)
        job.audit_trail.append(
            _audit(
                "raw_archive_deferred",
                actor="system",
                at=now,
                details={
                    "error": str(exc),
                    "retry_seconds": retry,
                    "attempt": job.stability_check_attempts,
                },
            )
        )
        return _result(
            job,
            [build_copying_retry_message(job.import_job_id, retry, job.owner_node_id)],
        )
    except (StaleImportLeaseError, RetryableImportError):
        # Retry/reclaim belongs to the durable owner-affine worker. Turning an
        # infrastructure error into a detached terminal result could mark a
        # partial import as complete or overwrite a newer lease generation.
        raise
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
    # Guard: already terminal — re-confirming a done/ignored/needs_context job must not requeue it.
    # Use /imports/{id}/retry to intentionally reprocess.
    if job.status in (ImportJobStatus.done, ImportJobStatus.ignored, ImportJobStatus.needs_operator_context):
        return _result(job, [])
    now = now or datetime.now(timezone.utc)
    job.confirmed_by = actor
    job.confirmed_at = now
    job.status = ImportJobStatus.checking_stability
    job.lease_owner = None
    job.lease_until = None
    job.updated_at = now
    job.audit_trail.append(_audit("confirm_requested", actor=actor, at=now))
    job.audit_trail.append(_audit("queued_for_worker", actor="system", at=now))
    return _result(job, [])


def queue_import_job_retry(
    job: ImportJobRecord,
    actor: str = "operator",
    now: datetime | None = None,
) -> ImportExecutionResult:
    """Requeue a terminal import for its owning PC without parsing in the API.

    Parsing is CPU- and IO-heavy and must remain in the local import worker.
    This function only changes durable state; the owner-affine worker claims it
    later.  Previous output rows are deterministic upserts, while these lists
    describe the new attempt and therefore start empty.
    """
    now = now or datetime.now(timezone.utc)
    job.status = ImportJobStatus.checking_stability
    job.confirmed_by = actor
    job.confirmed_at = now
    job.postponed_until = None
    job.lease_owner = None
    job.lease_until = None
    job.stability_check_attempts = 0
    job.session_ids = []
    job.report_ids = []
    job.missing_context_questions = []
    job.error = None
    job.updated_at = now
    job.audit_trail.append(_audit("retry_queued_for_owner", actor=actor, at=now))
    return _result(job, [])


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
    paths = [path for path in iter_source_files(source_path) if path.is_file()]
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


@contextmanager
def _prepared_import_sources(
    job: ImportJobRecord,
    *,
    settings: Settings,
    now: datetime,
    lease_guard: Callable[[], bool] | None,
) -> Iterator[tuple[Path, dict[str, str]]]:
    """Archive before parsing; keep extracted files alive until analysis ends."""
    source_path = Path(job.source_path)
    work_root = source_path
    cleanup: tempfile.TemporaryDirectory[str] | None = None
    try:
        _require_current_lease(lease_guard)
        # NAS is the durable source of truth, but never the compute node.  The
        # local worker streams the immutable ZIP before even extracting it. A
        # folder/file batch is likewise archived before its parser is called.
        if job.source_kind == "zip":
            job.source_objects = archive_raw_import(
                job,
                source_path,
                source_path,
                required=settings.app_env != "test",
            )
        else:
            job.checksum_manifest = calculate_checksum_manifest(work_root)
            job.source_objects = archive_raw_import(
                job,
                work_root,
                source_path,
                checksum_manifest=job.checksum_manifest,
                required=settings.app_env != "test",
            )
        archive_members = {}
        if job.source_kind == "zip" or any(
            path.suffix.lower() == '.zip'
            for path in iter_source_files(work_root) if path.is_file()
        ):
            from domain.services.log_archives import expand_log_inputs

            cleanup = tempfile.TemporaryDirectory(prefix="printer-log-import-")
            work_root = Path(cleanup.name)
            expanded_objects, archive_members, job.checksum_manifest = expand_log_inputs(
                source_path, work_root, job.source_objects,
                lease_check=lambda: _require_current_lease(lease_guard),
            )
            job.source_objects.update(expanded_objects)
        job.audit_trail.append(
            _audit(
                "raw_logs_archived_to_nas",
                actor="system",
                at=now,
                details={"object_count": len(job.source_objects)},
            )
        )

        job.audit_trail.append(
            _audit(
                "checksums_calculated",
                actor="system",
                at=now,
                details={"file_count": len(job.checksum_manifest)},
            )
        )

        yield work_root, archive_members
    finally:
        if cleanup is not None:
            cleanup.cleanup()


def _prepare_group_artifacts(
    group: SessionGroup,
    timing_manifest: dict[str, Any],
    profile: PrinterProfilePlugin | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare one shared analysis and its projections, without SQL or NAS writes."""
    from domain.services.session_analysis import prepare_session_analysis
    from domain.services.session_overview import build_group_overview

    analysis = prepare_session_analysis(group.files, profile=profile)
    overview = build_group_overview(
        group.group_id, group.files,
        start_ts=group.start_ts, end_ts=group.end_ts,
        grouping_confidence=float(group.confidence) if group.confidence else 0.0,
        analysis=analysis,
    )
    overview["timing_publication_id"] = timing_manifest["publication_id"]
    payload = {
        "files": [file.model_dump(mode="json", exclude={"parse_result"}) for file in group.files],
        "group": overview,
    }
    report = generate_session_json_report(
        group.group_id, group.files, analysis=analysis, overview=overview,
    )
    report["timing_publication"] = timing_manifest
    report["markdown"] = generate_markdown_report(report)
    return payload, report


def execute_confirmed_import(
    job: ImportJobRecord,
    registry: ParserRegistry,
    profile: PrinterProfilePlugin | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    lease_guard: Callable[[], bool] | None = None,
) -> ImportExecutionResult:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    fence = (ImportFence(job.import_job_id, job.owner_node_id, job.lease_owner, job.lease_generation)
             if job.lease_owner and job.lease_generation > 0 else None)
    if fence is None and settings.app_env != "test":
        raise StaleImportLeaseError("Импорт должен выполняться через очередь с действующим правом на задание.")

    job.status = ImportJobStatus.importing
    job.updated_at = now
    with _prepared_import_sources(
        job, settings=settings, now=now, lease_guard=lease_guard,
    ) as (work_root, archive_members):
        ingest_result = IngestionService(registry, profile).parse(work_root)
        for item in ingest_result.files:
            if item.checksum != job.checksum_manifest.get(item.relative_path):
                raise RetryableImportError(
                    "Исходный лог изменился между архивированием и разбором; импорт будет повторён."
                )
            if item.relative_path in archive_members:
                item.metadata['archive_member_path'] = archive_members[item.relative_path]
        _require_current_lease(lease_guard)
        groups = group_files_into_sessions(ingest_result.files)
        sessions: dict[str, dict[str, Any]] = {}
        reports: dict[str, dict[str, Any]] = {}
        layer_timings: dict[str, PreparedLayerTimings] = {}
        previous_session_tokens: dict[str, str] = {}
        # Retried jobs describe THIS attempt, not the union with stale attempts.
        job.session_ids = []
        job.report_ids = []
        job.missing_context_questions = []

        job.status = ImportJobStatus.analyzing
        job.updated_at = now
        for group in groups:
            _require_current_lease(lease_guard)
            # Use the deterministic group id so this (watcher/confirmation) path
            # converges with the startup/upload import paths — same print → same
            # session id → deduplicated, not a parallel duplicate.
            session_id = group.group_id

            # Create session record in DB first so FK constraints are satisfied
            previous_session_tokens[session_id] = _ensure_session_record(
                session_id,
                float(group.confidence) if group.confidence else 0.0,
                origin_compute_node_id=job.owner_node_id,
                fence=fence,
            )

            # Persist parse results (source files and canonical events) to database
            files_saved, events_saved = persist_parse_results_to_db(
                session_id,
                group.files,
                source_objects=job.source_objects,
                now=now,
                lease_guard=lease_guard,
                fence=fence,
            )
            logger.info(
                "Persisted parse results for session %s: %d files, %d events",
                session_id, files_saved, events_saved
            )

            # Prepare only. Existing visible rows and the explicit empty marker
            # are replaced later, together with the final overview and fence.
            from analytics.prediction.timing_snapshot import prepare_layer_timings
            from domain.services.session_sources import mirror_logs_to_object_store

            _require_current_lease(lease_guard)
            layer_timings[session_id] = prepare_layer_timings(
                group.files, owner_node_id=job.owner_node_id,
                source=fence.as_dict() if fence else {"mode": "test"},
            )
            mirror_logs_to_object_store(session_id, group.files, immutable=True)

            payload, report = _prepare_group_artifacts(
                group, layer_timings[session_id].manifest, profile,
            )
            sessions[session_id] = payload
            reports[report["report_id"]] = report
            job.session_ids.append(session_id)
            job.report_ids.append(report["report_id"])
            job.missing_context_questions.extend(build_missing_context_questions(session_id, report))

        job.status = ImportJobStatus.reporting
        job.updated_at = now
        # Context questions (material / powder batch / gas cylinder) are recorded
        # for display but do NOT block the import — the operator fills them in
        # separately via print records. Always mark as done here.
        final_status = ImportJobStatus.done
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
            job.owner_node_id,
        )
        return _result(job, [notification], sessions=sessions, reports=reports,
                       layer_timings=layer_timings, previous_session_tokens=previous_session_tokens)


def calculate_checksum_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(iter_source_files(root)):
        if path.is_file():
            relative = path.name if root.is_file() else str(path.relative_to(root))
            manifest[relative] = sha256_file(path)
    return manifest


def archive_raw_import(
    job: ImportJobRecord,
    work_root: Path,
    original_source: Path | None = None,
    *,
    checksum_manifest: dict[str, str] | None = None,
    required: bool = True,
) -> dict[str, str]:
    """Stream a complete immutable raw-log batch to NAS object storage.

    Direct files/folders are stored file-by-file so their ``SourceFile`` rows
    can point to exact objects. A ZIP remains one immutable original archive;
    extracted members are only a local working copy and are not duplicated on
    the NAS.
    """
    from storage.object_store.minio_client import ObjectStore

    store = ObjectStore()
    if not store.is_available():
        if not required:
            return {}
        raise RawArchiveUnavailableError(
            "NAS object storage is unavailable; raw logs were not archived"
        )
    bucket = store.settings.minio_bucket_raw
    prefix = f"imports/{job.owner_node_id}/{job.import_job_id}"

    if job.source_kind == "zip":
        archive = original_source or work_root
        if not archive.is_file():
            raise RuntimeError(f"Raw ZIP source is missing: {archive}")
        try:
            checksum = sha256_file(archive)
            return {
                "__source_archive__": store.put_file_verified(
                    bucket,
                    f"{prefix}/source/{checksum}/{archive.name}",
                    archive,
                    expected_sha256=checksum,
                    expected_size=archive.stat().st_size,
                )
            }
        except Exception as exc:
            raise RawArchiveUnavailableError(
                f"NAS rejected raw archive {archive.name}: {exc}"
            ) from exc

    paths = [path for path in sorted(iter_source_files(work_root)) if path.is_file()]
    if not paths:
        raise RuntimeError(f"Raw log source is empty: {work_root}")

    objects: dict[str, str] = {}
    checksums = checksum_manifest or calculate_checksum_manifest(work_root)
    try:
        for path in paths:
            relative = (
                path.name if work_root.is_file() else path.relative_to(work_root).as_posix()
            )
            checksum = checksums[relative]
            objects[relative] = store.put_file_verified(
                bucket,
                f"{prefix}/files/{checksum}/{Path(relative).name}",
                path,
                expected_sha256=checksum,
                expected_size=path.stat().st_size,
            )
    except Exception as exc:
        raise RawArchiveUnavailableError(
            f"NAS rejected raw log {path.name}: {exc}"
        ) from exc
    return objects


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
    layer_timings: dict[str, PreparedLayerTimings] | None = None,
    previous_session_tokens: dict[str, str] | None = None,
) -> ImportExecutionResult:
    for notification in notifications:
        job.notification_log.append(notification.model_dump(mode="json"))
    return ImportExecutionResult(
        job=job,
        notifications=notifications,
        sessions=sessions or {},
        reports=reports or {},
        layer_timings=layer_timings or {},
        previous_session_tokens=previous_session_tokens or {},
    )


def _audit(action: str, actor: str, at: datetime, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"action": action, "actor": actor, "timestamp": at.isoformat(), "details": details or {}}
