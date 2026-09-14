"""Durable attachments shared by HTTP upload and the local NAS synchronizer.

Transfer never holds a SQL connection. publish_attachment joins the caller's
short transaction; upload/delete use cases explicitly commit before receipts
or physical cleanup can escape to an HTTP response/background task.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import tempfile
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from core.config.settings import get_settings
from domain.models.prints import PrintRecordFile
from domain.services.estimation.inputs import require_local_print
from domain.services.estimation.requests import enqueue_estimate
from domain.services.print_cards.contracts import (
    AttachmentResult,
    CardError,
    DeletionResult,
    DownloadResult,
)
from parsers.common.timestamps import date_hint_datetime
from storage.db.session import session_scope
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository
from storage.sync.local_outbox import LocalNasOutbox, OutboxFullError

logger = logging.getLogger(__name__)
FILE_TYPES = {"stl", "stl_supports", "magics", "photo", "doc"}
MAX_UPLOAD_MB = 600
UPLOAD_CHUNK_BYTES = 1024 * 1024
GEOMETRY_TYPES = {"stl", "stl_supports"}


def bucket_for(file_type: str, settings=None) -> str:
    settings = settings or get_settings()
    return {
        "stl": settings.minio_bucket_stls,
        "stl_supports": settings.minio_bucket_stls,
        "magics": settings.minio_bucket_magics,
        "photo": settings.minio_bucket_photos,
        "doc": settings.minio_bucket_docs,
    }[file_type]


def stage_upload_to_file(source: BinaryIO, destination: Path, max_bytes: int) -> tuple[int, str]:
    """Bounded-memory staging and SHA-256; the enclosing temporary dir cleans up."""
    total = 0
    digest = hashlib.sha256()
    with destination.open("wb") as sink:
        while chunk := source.read(UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise CardError("too_large", f"Файл > {max_bytes // (1024 * 1024)} МБ")
            digest.update(chunk)
            sink.write(chunk)
    return total, digest.hexdigest()


def validate_attachment_record(item: dict, repo: PrintsRepository, *, for_update=False) -> dict:
    read = repo.get_print_record_for_update if for_update else repo.get_print_record
    record = read(str(item["record_id"]))
    if record is None:
        raise CardError("not_found", "Карточка печати не найдена")
    if item["file_type"] in GEOMETRY_TYPES:
        require_local_print(record, compute_node_id=str(item["owner_node_id"]))
    return record


def publish_object(store, item: dict, payload: Path) -> str:
    verified = getattr(store, "put_file_verified", None)
    if callable(verified):
        return verified(
            item["bucket"],
            item["object_name"],
            payload,
            expected_sha256=item["checksum"],
            expected_size=int(item["size_bytes"]),
            content_type=item["content_type"],
        )
    # Compatibility for adapters; the durable local payload is already verified.
    return store.put_file(
        item["bucket"],
        item["object_name"],
        payload,
        content_type=item["content_type"],
    )


def publish_attachment(repo: PrintsRepository, item: dict, object_uri: str) -> dict:
    """Fresh parent state + attachment + optional estimate in one transaction."""
    record = validate_attachment_record(item, repo, for_update=True)
    saved = repo.add_print_file(
        {
            "file_id": item["file_id"],
            "record_id": item["record_id"],
            "object_uri": object_uri,
            "file_name": item["file_name"],
            "file_type": item["file_type"],
            "size_bytes": item["size_bytes"],
            "checksum": item["checksum"],
        }
    )
    if saved.get("duplicate"):
        return saved
    # The pre-transfer copy is not authoritative after minutes of network IO.
    # Preserve a date or context entered by another operator during upload.
    if not record.get("printed_at"):
        hint = date_hint_datetime(str(item["file_name"]))
        if hint:
            repo.update_print_record(item["record_id"], {"printed_at": hint})
    if item["file_type"] in GEOMETRY_TYPES:
        current = repo.get_print_record(item["record_id"])
        enqueue_estimate(repo, current, compute_node_id=str(item["owner_node_id"]))
    return saved


def upload_attachment(
    repo: PrintsRepository,
    record_id: str,
    source: BinaryIO,
    *,
    file_name: str | None,
    file_type: str,
    settings=None,
    store_factory=None,
    max_bytes: int | None = None,
) -> AttachmentResult:
    """Blocking use case; async transports must run it off their event loop."""
    settings = settings or get_settings()
    store_factory = store_factory or ObjectStore
    if file_type not in FILE_TYPES:
        raise CardError(
            "invalid_inputs", f"Недопустимый file_type. Допустимы: {', '.join(sorted(FILE_TYPES))}"
        )
    file_name = (file_name or "unknown").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if len(file_name) > 300:
        raise CardError("invalid_inputs", "Имя файла слишком длинное (макс. 300 символов)")
    if file_type == "stl" and file_name.lower().startswith("s_"):
        file_type = "stl_supports"

    with tempfile.TemporaryDirectory(prefix="printer-upload-") as temporary_dir:
        staged_path = Path(temporary_dir) / "payload"
        source.seek(0)
        size_bytes, checksum = stage_upload_to_file(
            source,
            staged_path,
            max_bytes if max_bytes is not None else MAX_UPLOAD_MB * 1024 * 1024,
        )
        if not size_bytes:
            raise CardError("invalid_inputs", "Пустой файл")
        try:
            validate_attachment_record(
                {
                    "record_id": record_id,
                    "file_type": file_type,
                    "owner_node_id": settings.compute_node_id,
                },
                repo,
            )
            existing = repo.find_file_by_checksum(record_id, checksum)
        except SQLAlchemyError as exc:
            # A card may already be open when the NAS goes offline. Keep a
            # durable local copy; publication revalidates existence/ownership.
            logger.warning("PostgreSQL unavailable during upload pre-check: %s", exc)
            existing = None
        finally:
            repo.db.rollback()
        if existing:
            return AttachmentResult({"duplicate": True, **existing})

        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        try:
            outbox = LocalNasOutbox.from_settings(settings)
            queued = outbox.enqueue_attachment(
                staged_path,
                owner_node_id=settings.compute_node_id,
                record_id=record_id,
                file_name=file_name,
                file_type=file_type,
                bucket=bucket_for(file_type, settings),
                checksum=checksum,
                size_bytes=size_bytes,
                content_type=content_type,
                reopen_completed=True,
            )
        except OutboxFullError as exc:
            raise CardError(
                "insufficient_storage",
                "Локальная очередь NAS заполнена; освободите место или дождитесь синхронизации",
            ) from exc
        except OSError as exc:
            raise CardError(
                "insufficient_storage", "Не удалось сохранить локальную страховочную копию загрузки"
            ) from exc

    operation_id = str(queued["operation_id"])

    def postponed(reason: str) -> AttachmentResult:
        state = outbox.get(operation_id) or queued
        return AttachmentResult(
            {
                "queued": True,
                "sync_operation_id": operation_id,
                "sync_status": state.get("status", "pending"),
                "file_name": file_name,
                "file_type": file_type,
                "checksum": checksum,
                "size_bytes": size_bytes,
                "message": reason,
            },
            queued=True,
        )

    claimed = outbox.claim(operation_id)
    if claimed is None:
        return postponed("Файл уже находится в локальной очереди синхронизации")
    try:
        queued_path = outbox.verify_claimed(claimed)
        store = store_factory()
        if not store.is_available():
            outbox.release(operation_id, "Хранилище файлов MinIO недоступно")
            return postponed(
                "NAS недоступен: файл сохранён на этом ПК и будет отправлен автоматически"
            )
        object_uri = publish_object(store, claimed, queued_path)
    except Exception as exc:
        outbox.release(operation_id, str(exc))
        logger.warning("upload queued after MinIO failure: %s", exc)
        return postponed(
            "Передача на NAS прервалась: локальная копия сохранена и будет отправлена повторно"
        )

    try:
        saved = publish_attachment(repo, claimed, object_uri)
        repo.db.commit()
    except SQLAlchemyError as exc:
        repo.db.rollback()
        outbox.release(operation_id, str(exc))
        logger.warning("database publication postponed: %s", exc)
        return postponed(
            "База NAS временно недоступна: файл сохранён локально и будет опубликован автоматически"
        )
    except Exception as exc:
        repo.db.rollback()
        outbox.fail(operation_id, str(exc))
        remove_unreferenced_objects([object_uri], store_factory=lambda: store)
        raise
    if saved.get("duplicate") and saved["object_uri"] != object_uri:
        remove_unreferenced_objects([object_uri], store_factory=lambda: store)
    outbox.complete(operation_id, saved)
    logger.info("attached %s (%s, %d bytes) to %s", file_name, file_type, size_bytes, record_id)
    return AttachmentResult(
        saved, new_geometry=file_type in GEOMETRY_TYPES and not saved.get("duplicate")
    )


def remove_unreferenced_objects(uris: list[str], *, store_factory=None) -> None:
    """Best effort after a confirmed commit; retain shared or unverifiable files."""
    if not uris:
        return
    try:
        with session_scope() as db:
            referenced = set(
                db.scalars(
                    select(PrintRecordFile.object_uri).where(PrintRecordFile.object_uri.in_(uris))
                ).all()
            )
        store = (store_factory or ObjectStore)()
        for uri in set(uris) - referenced:
            bucket, _, name = uri.removeprefix("s3://").partition("/")
            if not store.remove_object(bucket, name):
                logger.warning("could not remove unreferenced attachment %s", uri)
    except Exception:
        # A cleanup failure must never turn a committed deletion into a lost
        # card/file pair. Retention is safer than guessing while NAS is down.
        logger.exception("attachment cleanup deferred; remote objects retained")


def delete_attachment(repo: PrintsRepository, record_id: str, file_id: str) -> DeletionResult:
    try:
        if not repo.get_print_record_for_update(record_id):
            raise CardError("not_found", "Файл не найден")
        uri = repo.delete_print_file(record_id, file_id)
        if uri is None:
            raise CardError("not_found", "Файл не найден")
        repo.flush()
        repo.db.commit()
    except Exception:
        repo.db.rollback()
        raise
    return DeletionResult({"deleted": file_id}, [uri])


def download_attachment(
    repo: PrintsRepository, record_id: str, file_id: str, *, store_factory=None
) -> DownloadResult:
    """Read-only use case on a clean repository; release SQL before streaming."""
    match = next((f for f in repo.list_print_files(record_id) if f["file_id"] == file_id), None)
    repo.db.rollback()
    if not match:
        raise CardError("not_found", "Файл не найден")
    bucket, _, object_name = match["object_uri"].removeprefix("s3://").partition("/")
    stream = (store_factory or ObjectStore)().open_stream(bucket, object_name)
    if stream is None:
        raise CardError("storage_unavailable", "Файл недоступен в хранилище")
    return DownloadResult(
        stream,
        match["file_name"],
        mimetypes.guess_type(match["file_name"])[0] or "application/octet-stream",
        match.get("size_bytes"),
    )
