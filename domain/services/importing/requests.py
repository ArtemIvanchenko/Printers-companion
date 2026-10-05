"""Import registration and operator actions, without running an import.

Prepare every candidate before opening the caller's publishing transaction.
Publication only changes SQL rows and notification outbox entries; parsing,
archival and analytical results belong to the owner-local worker.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.config.settings import Settings
from domain.services.import_jobs import (
    calculate_checksum_manifest,
    detect_import_candidate,
    ignore_import_job,
    mark_import_job_confirmed,
    postpone_import_job,
    queue_import_job_retry,
    snapshot_source,
)
from domain.services.importing.contracts import ImportExecutionResult, ImportJobRecord
from storage.db.session import SessionLocal, session_scope
from storage.repositories.import_jobs import ImportJobsRepository
from storage.repositories.runtime import RuntimeRepository


class ImportRequestError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PreparedImportCandidate:
    path: Path
    file_snapshot: dict
    checksum_manifest: dict[str, str] | None


def prepare_import_candidate(source_path: str | Path, *, settings: Settings) -> PreparedImportCandidate:
    """Detach local filesystem work from the subsequent SQL publication.

    The small name probe closes before hashing. A manifest is only needed when
    a completed same-name import could identify a copy at a different path.
    """
    path = Path(source_path).resolve(strict=False)
    snapshot = snapshot_source(path)
    with SessionLocal() as db:
        has_name_candidate = ImportJobsRepository(db).has_terminal_import_job_by_name(
            owner_node_id=settings.compute_node_id, source_name=path.name,
        )
    manifest = calculate_checksum_manifest(path) if has_name_candidate and path.exists() else None
    return PreparedImportCandidate(path, snapshot, manifest)


def _persist_request(db, result: ImportExecutionResult) -> None:
    # Requests never publish calculated sessions or reports. Keeping those
    # writes here would provide a second, unfenced import publication path.
    ImportJobsRepository(db).save_import_job(result.job)
    RuntimeRepository(db).save_notifications(result.notifications)
    db.flush()


def publish_import_candidate(
    db,
    candidate: PreparedImportCandidate,
    *,
    settings: Settings,
    print_record_id: str | None = None,
) -> ImportExecutionResult:
    """SQL-only registration; the caller owns commit/rollback.

    Advisory and row locks preserve a running lease when watcher detection is
    followed by a stronger explicit card link. Prepared bytes are only a hint:
    the worker still checks stability and SHA before calculation/publication.
    """
    repo = ImportJobsRepository(db)
    source_path = str(candidate.path)
    repo.lock_import_candidate(settings.compute_node_id, source_path)
    for listed in repo.list_import_jobs_by_source_path(
        owner_node_id=settings.compute_node_id, source_path=source_path,
    ):
        existing = repo.get_import_job_for_update(listed.import_job_id)
        if existing is None:
            continue
        terminal = existing.status in ("done", "needs_operator_context", "ignored", "failed")
        if terminal and existing.file_snapshot != candidate.file_snapshot:
            continue
        if print_record_id and existing.print_record_id is None:
            existing.print_record_id = print_record_id
            repo.save_import_job(existing)
            db.flush()
        elif print_record_id and existing.print_record_id != print_record_id:
            raise ImportRequestError("conflict", "Import batch is already attached to another print record")
        return ImportExecutionResult(job=existing, notifications=[])
    if candidate.checksum_manifest is not None:
        for existing in repo.list_terminal_import_jobs_by_name(
            owner_node_id=settings.compute_node_id, source_name=candidate.path.name,
        ):
            if existing.checksum_manifest and candidate.checksum_manifest == existing.checksum_manifest:
                return ImportExecutionResult(job=existing, notifications=[])
    result = detect_import_candidate(
        candidate.path, settings=settings, print_record_id=print_record_id,
        file_snapshot=candidate.file_snapshot,
    )
    _persist_request(db, result)
    return result


def register_import_candidates(
    source_paths: list[str | Path],
    *,
    settings: Settings,
    print_record_id: str | None = None,
) -> list[ImportExecutionResult]:
    """Prepare all local inputs, then atomically register the complete batch.

    This use case owns a clean Session. There is no optional outer transaction
    that could remain open while the next candidate walks or hashes files.
    """
    candidates = [prepare_import_candidate(path, settings=settings) for path in source_paths]
    with session_scope() as db:
        return [publish_import_candidate(db, candidate, settings=settings,
                                         print_record_id=print_record_id)
                for candidate in candidates]


def get_import_job(db, import_job_id: str, *, settings: Settings, for_update: bool = False) -> ImportJobRecord:
    repo = ImportJobsRepository(db)
    job = repo.get_import_job_for_update(import_job_id) if for_update else repo.get_import_job(import_job_id)
    if job is None or job.owner_node_id != settings.compute_node_id:
        raise ImportRequestError("not_found", "Import job not found")
    return job


def apply_import_action(
    db,
    import_job_id: str,
    action: str,
    *,
    settings: Settings,
    actor: str = "operator",
    retry_seconds: int | None = None,
) -> ImportExecutionResult:
    """One locked state transition for REST and notification callbacks."""
    job = get_import_job(db, import_job_id, settings=settings, for_update=True)
    lease_until = job.lease_until
    if lease_until is not None and job.lease_owner:
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if lease_until > datetime.now(timezone.utc):
            raise ImportRequestError("conflict", "Import is already running on its owner PC; wait for it to finish")
    if action == "confirm":
        result = mark_import_job_confirmed(job, actor=actor)
    elif action == "ignore":
        result = ignore_import_job(job, actor=actor)
    elif action == "postpone":
        result = postpone_import_job(job, actor=actor, settings=settings, retry_seconds=retry_seconds)
    elif action == "retry":
        result = queue_import_job_retry(job, actor=actor)
    else:
        raise ImportRequestError("invalid_inputs", "Unsupported import callback action")
    _persist_request(db, result)
    return result


def apply_import_callback(db, callback_data: str, *, settings: Settings, actor: str = "operator") -> ImportExecutionResult:
    try:
        prefix, import_job_id, action = callback_data.split(":", 2)
    except ValueError as exc:
        raise ImportRequestError("invalid_inputs", "Invalid callback data") from exc
    if prefix != "import":
        raise ImportRequestError("invalid_inputs", "Unsupported callback prefix")
    return apply_import_action(db, import_job_id, action, settings=settings, actor=actor)
