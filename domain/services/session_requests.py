"""Durable session-analysis requests through the existing local import worker.

There is no second report worker: replaying the original import publishes the
overview, compact measurements and reports together under its normal fence.
"""

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from domain.enums.common import ImportJobStatus
from domain.models.sessions import ImportJob
from domain.services.compute_affinity import ComputeAffinityError
from domain.services.import_jobs import (
    detect_import_candidate, mark_import_job_confirmed, queue_import_job_retry,
)
from domain.services.session_reports import SessionReportError
from storage.repositories.runtime import RuntimeRepository


_TERMINAL = {ImportJobStatus.done, ImportJobStatus.needs_operator_context,
             ImportJobStatus.ignored, ImportJobStatus.failed}


def _response(job, *, session_id: str | None = None) -> dict:
    return {"contract_version": 2, "status": "queued", "job_status": job.status,
            "job_id": job.import_job_id, "import_job_id": job.import_job_id,
            "owner_node_id": job.owner_node_id, "session_id": session_id,
            "status_url": f"/imports/{job.import_job_id}"}


def _enqueue(repo: RuntimeRepository, job, *, actor: str, session_id: str | None = None):
    now = datetime.now(timezone.utc)
    until = job.lease_until
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if job.lease_owner and until and until > now:
        return job  # Do not replace the token of a running local worker.
    previous_sessions = list(job.session_ids)
    if job.status in _TERMINAL:
        result = queue_import_job_retry(job, actor=actor)
    elif job.status in (ImportJobStatus.detected, ImportJobStatus.awaiting_operator_confirmation):
        result = mark_import_job_confirmed(job, actor=actor)
    else:
        return job  # Already queued/postponed: preserve its retry/lease state.
    if session_id or previous_sessions:
        result.job.audit_trail.append({
            "action": "session_analysis_requested", "actor": actor,
            "timestamp": now.isoformat(), "details": {
                "session_id": session_id, "session_ids": previous_sessions,
            },
        })
    repo.save_import_job(result.job)
    repo.save_notifications(result.notifications)
    return result.job


def request_ingest(db, payload: dict, *, settings, actor: str = "operator") -> dict:
    raw = payload.get("folder") or payload.get("path")
    if not isinstance(raw, str) or not raw.strip():
        raise SessionReportError("invalid_inputs", "Укажите folder или path внутри локальной папки логов.")
    if payload.get("session_id"):
        raise SessionReportError("invalid_inputs", "Контракт v2: идентификаторы сессий определяет импортёр; session_id не задаётся вручную.")
    source = Path(raw).expanduser().resolve(strict=False)
    root = Path(settings.raw_logs_container_path).expanduser().resolve(strict=False)
    if not source.is_relative_to(root) or any(part.startswith(".browser-upload-") for part in source.relative_to(root).parts):
        raise SessionReportError("forbidden", "Импорт разрешён только из локальной папки логов; приватные браузерные пакеты обрабатывает их собственное задание.")
    # No recursive walk/hash/parse in the request. The worker verifies file
    # stability and the immutable raw archive before producing any results.
    try:
        repo = RuntimeRepository(db)
        repo.lock_import_candidate(settings.compute_node_id, str(source))
        rows = repo.list_import_jobs_by_source_path(
            owner_node_id=settings.compute_node_id, source_path=str(source),
        )
        if rows:
            job = repo.get_import_job_for_update(rows[0].import_job_id)
        else:
            result = detect_import_candidate(source, settings=settings, file_snapshot={})
            job = result.job
        job = _enqueue(repo, job, actor=actor)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise SessionReportError("storage_unavailable", "Не удалось подтвердить запись задания на NAS. Исходники не изменены; повторите запрос после восстановления связи.") from exc
    except Exception:
        db.rollback()
        raise
    return {**_response(job), "root": str(source), "groups": [], "skipped": [], "diagnostics": []}


def _source_job_id(db, session_id: str, context: dict, owner: str) -> str | None:
    # Modern publications retain a direct, unambiguous job identity even while
    # a retry clears the job's previous session_ids/result lists.
    source = (context.get("timing_publication") or {}).get("source") or {}
    if source.get("import_job_id"):
        return str(source["import_job_id"])
    # Legacy rows have no manifest; inspect only the small linkage columns,
    # never their filesystem paths or source payloads. The audit link survives
    # the first retry, unlike session_ids.
    for job_id, session_ids, audit in db.execute(select(
        ImportJob.import_job_id, ImportJob.session_ids, ImportJob.audit_trail,
    ).where(ImportJob.owner_node_id == owner).order_by(ImportJob.updated_at.desc())):
        if session_id in (session_ids or []) or any(
            item.get("action") == "session_analysis_requested"
            and ((item.get("details") or {}).get("session_id") == session_id
                 or session_id in ((item.get("details") or {}).get("session_ids") or []))
            for item in (audit or [])
        ):
            return job_id
    return None


def request_analysis(db, session_id: str, *, compute_node_id: str,
                     actor: str = "operator") -> dict:
    try:
        repo = RuntimeRepository(db)
        try:
            session = repo.require_session_compute_owner(session_id, requested_compute_node_id=compute_node_id)
        except ComputeAffinityError as exc:
            raise SessionReportError("forbidden", "Пересчёт разрешён только на ПК-владельце сессии.") from exc
        if session is None:
            raise SessionReportError("not_found", "Сессия не найдена")
        job_id = _source_job_id(db, session_id, session.context or {}, compute_node_id)
        job = repo.get_import_job_for_update(job_id) if job_id else None
        if job is None or job.owner_node_id != compute_node_id:
            raise SessionReportError("conflict", "У сессии нет исходного задания импорта этого ПК. Загрузите логи заново из карточки; путь к старым файлам не угадывается.")
        job = _enqueue(repo, job, actor=actor, session_id=session_id)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise SessionReportError("storage_unavailable", "Не удалось подтвердить постановку анализа на NAS. Проверьте статус импорта после восстановления связи.") from exc
    except Exception:
        db.rollback()
        raise
    return _response(job, session_id=session_id)
