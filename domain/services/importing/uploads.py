"""Browser log intake: local preparation followed by a short SQL publication.

Private batches live on the shared raw-log mount, but only their explicit import
job may read them. Watcher/rescan of an ancestor must skip BROWSER_UPLOAD_PREFIX.
An uncertain commit retains the bytes and a local receipt; it never exposes an
unlinked batch to the watcher or deletes a possibly committed job's inputs.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import BinaryIO, Iterable
import unicodedata

from sqlalchemy.exc import SQLAlchemyError

from core.config.settings import Settings
from core.utils.files import BROWSER_UPLOAD_PREFIX
from domain.services.estimation.contracts import EstimateError
from domain.services.estimation.inputs import require_local_print
from domain.services.import_jobs import detect_import_candidate, snapshot_source
from domain.services.print_cards.contracts import CardError
from parsers.common.timestamps import date_hint_datetime
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import RuntimeRepository

logger = logging.getLogger(__name__)
ALLOWED_SUFFIXES = frozenset({".log", ".zip"})
MAX_FILE_MB = 2000
_CHUNK_BYTES = 1024 * 1024
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
}


@dataclass(frozen=True)
class LogUpload:
    filename: str | None
    source: BinaryIO


class _UploadNameTooLong(ValueError):
    pass


def _portable_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if ord(ch) < 32 or ch in '<>:"/\\|?*' else ch for ch in name)
    name = name.rstrip(". ")
    suffix = Path(name).suffix
    # Do not truncate a date or a family suffix such as _time / _Monitor100:
    # both are parser inputs, not decoration. Overlong names are reported.
    stem = Path(name).stem
    if stem.split(".", 1)[0].upper() in _WINDOWS_RESERVED or name.startswith(BROWSER_UPLOAD_PREFIX):
        stem = "_" + stem
    return stem + suffix


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _choose_target(batch: Path, name: str, checksum: str, stored: dict) -> tuple[Path, bool]:
    """Resolve every collision, including collisions with generated names."""
    base = _portable_name(name)
    candidate, attempt = base, 0
    while True:
        if len(candidate.encode("utf-8")) > 255:
            raise _UploadNameTooLong(candidate)
        previous = stored.get(_name_key(candidate))
        if previous is not None and previous[1] == checksum:
            return previous[0], True
        target = batch / candidate
        if previous is None and not target.exists():
            return target, False
        attempt += 1
        # Preserve the original family suffix. A hex checksum prefix can itself
        # resemble a date to date_hint_from_filename, so use a simple counter.
        candidate = f"copy_{attempt}__{base}"


def _require_card(repo: PrintsRepository, record_id: str, settings: Settings, *, locked=False):
    record = (
        repo.get_print_record_for_update(record_id) if locked else repo.get_print_record(record_id)
    )
    if not record:
        raise CardError("not_found", "Карточка печати не найдена")
    try:
        require_local_print(record, compute_node_id=settings.compute_node_id)
    except EstimateError as exc:
        raise CardError(exc.code, exc.detail) from exc
    return record


def _write_receipt(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _discard_batch(batch: Path | None, receipt: Path | None) -> None:
    # Only paths created by this invocation are passed here, never source files
    # or a directory read from an HTTP body / database record.
    try:
        if batch is not None:
            shutil.rmtree(batch)
        if receipt is not None:
            receipt.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove private upload copies at %s", batch)


def publish_prepared_browser_upload(
    repo: PrintsRepository, result, *, settings: Settings, printed_at_hint=None,
):
    """SQL-only registration shared by live intake and receipt recovery.

    The caller commits. Reconciliation may race the original request after an
    uncertain commit: never overwrite an existing job's status or worker lease.
    """
    runtime = RuntimeRepository(repo.db)
    job = result.job
    if job.owner_node_id != settings.compute_node_id:
        raise CardError("forbidden", "Публикация браузерного пакета разрешена только его ПК-владельцу.")
    runtime.lock_import_candidate(job.owner_node_id, job.source_path)
    existing = runtime.get_import_job_for_update(job.import_job_id)
    if existing is not None:
        if (existing.owner_node_id != job.owner_node_id
                or existing.source_path != job.source_path
                or existing.print_record_id != job.print_record_id
                or existing.checksum_manifest != job.checksum_manifest):
            raise CardError("conflict", "Задание из квитанции не соответствует сохранённому импорту; нужна проверка оператора.")
        return existing
    # A different job cannot take ownership of this private batch, either.
    other = runtime.list_import_jobs_by_source_path(owner_node_id=job.owner_node_id,
                                                    source_path=job.source_path)
    if other:
        raise CardError("conflict", "Локальный пакет уже связан с другим заданием импорта.")
    record = _require_card(repo, job.print_record_id, settings, locked=True) if job.print_record_id else None
    runtime.save_import_job(job)
    runtime.save_notifications(result.notifications)
    if record is not None and printed_at_hint:
        updates = {"metadata_json": {**(record.get("metadata_json") or {}),
                                     "log_import_hint": {"date": printed_at_hint.date().isoformat()}}}
        if not record.get("printed_at"):
            updates["printed_at"] = printed_at_hint
        repo.update_print_record(job.print_record_id, updates, expected_revision=record["revision"])
    return job


def upload_log_batch(
    repo: PrintsRepository,
    uploads: Iterable[LogUpload],
    *,
    settings: Settings,
    record_id: str | None = None,
    max_file_mb: int = MAX_FILE_MB,
) -> dict:
    """Own a clean session; accept a complete batch without running a parser.

    Call the whole use case off the event loop. Streams and SQL session must not
    be used concurrently by the caller. The worker on this PC handles archival,
    analysis and retry after a successfully published import job.
    """
    batch: Path | None = None
    receipt: Path | None = None
    retain = False
    try:
        try:
            if record_id is not None:
                _require_card(repo, record_id, settings)
        finally:
            repo.db.rollback()
        destination = Path(settings.raw_logs_container_path)
        if not destination.is_dir():
            raise CardError("log_directory_unavailable", f"Папка логов не найдена: {destination}")

        saved, skipped, stored = [], [], {}
        printed_at_hint = None
        for upload in uploads:
            # Browsers and direct clients can submit either Windows or POSIX paths.
            name = (upload.filename or "unknown").replace("\\", "/").rsplit("/", 1)[-1]
            if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
                continue
            if batch is None:
                incoming = destination / "incoming"
                incoming.mkdir(parents=True, exist_ok=True)
                kind = "print" if record_id is not None else "upload"
                prefix = (
                    BROWSER_UPLOAD_PREFIX
                    + kind
                    + "_"
                    + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_")
                )
                batch = Path(tempfile.mkdtemp(prefix=prefix, dir=incoming)).resolve()
            temporary = batch / ".part"
            total, digest, too_big = 0, hashlib.sha256(), False
            try:
                with temporary.open("xb") as output:
                    while chunk := upload.source.read(_CHUNK_BYTES):
                        total += len(chunk)
                        if total > max_file_mb * 1024 * 1024:
                            too_big = True
                            break
                        digest.update(chunk)
                        output.write(chunk)
                    if not too_big:
                        output.flush()
                        os.fsync(output.fileno())
                if too_big:
                    skipped.append({"name": name, "reason": f"файл > {max_file_mb} МБ"})
                    continue
                checksum = digest.hexdigest()
                try:
                    target, duplicate = _choose_target(batch, name, checksum, stored)
                except _UploadNameTooLong:
                    skipped.append({"name": name, "reason": "слишком длинное имя файла"})
                    continue
                if not duplicate:
                    temporary.rename(target)
                    stored[_name_key(target.name)] = (target, checksum)
                saved.append(
                    {
                        "name": name,
                        "stored_name": target.name,
                        "size_bytes": total,
                        "checksum": checksum,
                        "duplicate": duplicate,
                    }
                )
                printed_at_hint = printed_at_hint or date_hint_datetime(name)
            finally:
                temporary.unlink(missing_ok=True)

        if not saved:
            _discard_batch(batch, None)
            return {"saved": [], "skipped": skipped, "jobs": []}

        # Snapshot/hashing and recovery metadata precede all publication locks.
        result = detect_import_candidate(
            batch,
            settings=settings,
            print_record_id=record_id,
            file_snapshot=snapshot_source(batch),
        )
        result.job.checksum_manifest = {row["stored_name"]: row["checksum"] for row in saved}
        receipt = batch.with_name(batch.name + ".json")
        _write_receipt(
            receipt,
            {
                "schema_version": 2,
                "state": "prepared",
                "import_job_id": result.job.import_job_id,
                "owner_node_id": settings.compute_node_id,
                "print_record_id": record_id,
                "source_path": str(batch),
                "files": saved,
                # Preserve the original confirmation/audit policy on recovery;
                # changing workstation settings must not silently approve work.
                "job": result.job.model_dump(mode="json"),
                "notifications": [row.model_dump(mode="json") for row in result.notifications],
                "printed_at_hint": printed_at_hint.isoformat() if printed_at_hint else None,
            },
        )

        # No filesystem walking/copying while SQL is held. This is a new private
        # batch, so it cannot race a watcher's existing job for the same source.
        result.job = publish_prepared_browser_upload(repo, result, settings=settings,
                                                     printed_at_hint=printed_at_hint)
        retain = True  # A later failure may mean COMMIT succeeded remotely.
        repo.db.commit()
    except SQLAlchemyError as exc:
        repo.db.rollback()
        # Even when the final lock/read failed, preserve a complete prepared
        # batch for recovery; no watcher can import it without its card identity.
        if receipt is None:
            _discard_batch(batch, None)
        raise CardError(
            "storage_unavailable",
            "База данных недоступна. "
            + (
                f"Пакет сохранён локально. Обработчик этого ПК автоматически сверит задание по квитанции {receipt} после восстановления связи."
                if receipt is not None
                else "Загрузка не завершена; повторите после восстановления связи."
            ),
        ) from exc
    except BaseException as exc:
        repo.db.rollback()
        if isinstance(exc, CardError) and exc.code == "conflict":
            retain = True  # Another published job may already own these bytes.
        if not retain:
            _discard_batch(batch, receipt)
        if isinstance(exc, OSError):
            code = "insufficient_storage" if exc.errno == errno.ENOSPC else "storage_unavailable"
            raise CardError(code, "Не удалось записать пакет логов на локальный диск.") from exc
        raise

    # SQL is committed; a failed receipt cleanup must not turn success into an
    # ambiguous error. A leftover receipt is reconciled by its immutable job ID.
    try:
        receipt.unlink(missing_ok=True)
    except OSError:
        logger.warning("Committed log upload retained its receipt at %s", receipt)
    logger.info("Saved %d log uploads for card %s", len(saved), record_id)
    return {"saved": saved, "skipped": skipped, "jobs": [result.job.model_dump(mode="json")]}
