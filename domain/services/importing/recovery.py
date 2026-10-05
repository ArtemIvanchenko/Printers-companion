"""Reconcile private browser-upload receipts after an uncertain NAS commit.

Lookup by immutable job ID precedes file IO. Missing jobs are registered only
after local SHA/owner/card validation, using the same publication lock as HTTP.
Neither a source batch nor its files are ever removed or moved by recovery.
"""

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import time

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from core.config.settings import Settings
from core.utils.files import BROWSER_UPLOAD_PREFIX
from domain.enums.common import ImportJobStatus
from domain.services.importing.contracts import ImportExecutionResult, ImportJobRecord
from domain.services.importing.uploads import _name_key, _write_receipt, publish_prepared_browser_upload
from domain.services.print_cards.contracts import CardError
from operator_journal.notifications import NotificationMessage, build_import_confirmation_message
from storage.db.session import SessionLocal
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.import_jobs import ImportJobsRepository

logger = logging.getLogger(__name__)
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_INITIAL_STATUSES = {ImportJobStatus.detected, ImportJobStatus.awaiting_operator_confirmation}


class RecoveryConflict(ValueError):
    """Keep the receipt and all bytes; only an operator can resolve ambiguity."""


def _read_receipt(path: Path, settings: Settings) -> tuple[dict, Path]:
    incoming = Path(settings.raw_logs_container_path).resolve() / "incoming"
    if incoming.is_symlink() or path.is_symlink() or path.parent != incoming:
        raise RecoveryConflict("Квитанция должна находиться прямо в локальной папке incoming, без символических ссылок.")
    if not path.name.startswith(BROWSER_UPLOAD_PREFIX) or path.suffix != ".json":
        raise RecoveryConflict("Недопустимое имя квитанции браузерного пакета.")
    if path.stat().st_size > _MAX_RECEIPT_BYTES:
        raise RecoveryConflict("Квитанция превышает безопасный размер чтения.")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt.get("schema_version") not in (1, 2)):
        raise RecoveryConflict("Неизвестная версия квитанции.")
    if receipt.get("state") != "prepared" or not isinstance(receipt.get("recovery", {}), dict):
        raise RecoveryConflict("Повреждено состояние квитанции.")
    if receipt.get("owner_node_id") != settings.compute_node_id:
        raise RecoveryConflict("Квитанция принадлежит другому ПК; её файлы не открывались.")
    if not re.fullmatch(r"import_[0-9a-f]{32}", str(receipt.get("import_job_id") or "")):
        raise RecoveryConflict("Повреждён неизменяемый идентификатор задания.")
    batch = path.with_suffix("")
    if receipt.get("source_path") != str(batch) or batch.is_symlink():
        raise RecoveryConflict("Исходный путь не совпадает с соседним приватным пакетом.")
    return receipt, batch


def _expected_files(receipt: dict) -> dict[str, tuple[str, int]]:
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        raise RecoveryConflict("В квитанции отсутствует полный список файлов.")
    expected = {}
    portable = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise RecoveryConflict("Повреждена запись о файле.")
        name, checksum, size = entry.get("stored_name"), entry.get("checksum"), entry.get("size_bytes")
        if (not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..")
                or "\\" in name or "/" in name):
            raise RecoveryConflict("Имя файла выходит за границы приватного пакета.")
        if not re.fullmatch(r"[0-9a-f]{64}", str(checksum or "")) or type(size) is not int or size < 0:
            raise RecoveryConflict("Некорректные SHA-256 или размер файла.")
        value = (checksum, size)
        key = _name_key(name)
        if (name in expected and expected[name] != value) or (key in portable and portable[key] != name):
            raise RecoveryConflict("Квитанция содержит неоднозначные имена или контрольные суммы.")
        expected[name] = value
        portable[key] = name
    return expected


def _matches_job(job, receipt: dict, manifest: dict) -> bool:
    return (job.owner_node_id == receipt["owner_node_id"]
            and job.print_record_id == receipt.get("print_record_id")
            and job.source_path == receipt["source_path"]
            and job.checksum_manifest == manifest)


def _verify_batch(batch: Path, expected: dict) -> dict:
    if not batch.is_dir() or batch.is_symlink():
        raise RecoveryConflict("Приватный пакет отсутствует или заменён ссылкой.")
    paths = list(batch.iterdir())
    if {path.name for path in paths} != set(expected):
        raise RecoveryConflict("Состав пакета изменился после загрузки; автоматическое восстановление остановлено.")
    snapshot = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise RecoveryConflict("Пакет содержит ссылку или вложенный каталог вместо файла.")
        before = path.stat()
        checksum, size = expected[path.name]
        if before.st_size != size:
            raise RecoveryConflict("Размер файла не соответствует квитанции.")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (digest.hexdigest() != checksum or before.st_mtime_ns != after.st_mtime_ns
                or before.st_size != after.st_size or before.st_ino != after.st_ino):
            raise RecoveryConflict("SHA-256 файла изменился; требуется проверка оператора.")
        snapshot[path.name] = {"size": size, "mtime": after.st_mtime}
    return snapshot


def _prepared_result(receipt: dict, snapshot: dict, manifest: dict) -> tuple[ImportExecutionResult, datetime | None]:
    hint = None
    if receipt["schema_version"] == 2:
        job = ImportJobRecord.model_validate(receipt.get("job"))
        if (job.import_job_id != receipt["import_job_id"] or not _matches_job(job, receipt, manifest)
                or job.status not in _INITIAL_STATUSES or job.lease_owner or job.lease_generation):
            raise RecoveryConflict("Сохранённая регистрация не совпадает с неизменяемой квитанцией.")
        notifications = [NotificationMessage.model_validate(row) for row in receipt.get("notifications", [])]
        for notice in notifications:
            if (notice.owner_node_id != receipt["owner_node_id"]
                    or notice.metadata.get("import_job_id") != job.import_job_id):
                raise RecoveryConflict("Уведомление относится к другому заданию или ПК.")
        if receipt.get("printed_at_hint"):
            hint = datetime.fromisoformat(receipt["printed_at_hint"])
    else:
        # v1 did not persist confirmation policy. Never guess that the operator
        # had approved work or that today's auto-import setting also applied then.
        job = ImportJobRecord(import_job_id=receipt["import_job_id"],
                              owner_node_id=receipt["owner_node_id"],
                              print_record_id=receipt.get("print_record_id"),
                              source_path=receipt["source_path"], source_name=Path(receipt["source_path"]).name,
                              status=ImportJobStatus.awaiting_operator_confirmation,
                              checksum_manifest=manifest)
        notifications = [build_import_confirmation_message(job.import_job_id, job.source_name, job.owner_node_id)]
    job.file_snapshot = snapshot
    return ImportExecutionResult(job=job, notifications=notifications), hint


def _save_state(path: Path, receipt: dict, result: dict, *, now: float, settings: Settings) -> None:
    previous = receipt.get("recovery") or {}
    attempts = int(previous.get("attempts", 0)) + 1
    result = {**result, "attempts": attempts, "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    if result["state"] == "deferred":
        result["retry_after"] = now + min(settings.nas_sync_retry_max_seconds,
                                         settings.nas_sync_retry_min_seconds * 2 ** min(attempts - 1, 16))
    # The original HTTP request may already have removed its acknowledged
    # receipt. Do not resurrect it, and never mutate any source payload.
    if path.exists():
        _write_receipt(path, {**receipt, "recovery": result})


def recover_upload_receipt(path: Path, *, settings: Settings, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    receipt = None
    try:
        receipt, batch = _read_receipt(path, settings)
        previous = receipt.get("recovery") or {}
        if previous.get("state") in ("registered", "needs_attention") or float(previous.get("retry_after", 0)) > now:
            return {"state": "skipped", "job_id": receipt["import_job_id"]}
        expected = _expected_files(receipt)
        manifest = {name: value[0] for name, value in expected.items()}
        # COMMIT may have succeeded remotely even though HTTP reported failure.
        # Looking up this ID is the first external operation, before hashing.
        with SessionLocal() as db:
            existing = ImportJobsRepository(db).get_import_job(receipt["import_job_id"])
        if existing is not None:
            if not _matches_job(existing, receipt, manifest):
                raise RecoveryConflict("Идентификатор уже существует, но связь с пакетом или карточкой отличается.")
        else:
            snapshot = _verify_batch(batch, expected)
            prepared, hint = _prepared_result(receipt, snapshot, manifest)
            with SessionLocal() as db:
                publish_prepared_browser_upload(PrintsRepository(db), prepared, settings=settings,
                                                printed_at_hint=hint)
                db.commit()
        result = {"state": "registered", "job_id": receipt["import_job_id"]}
    except FileNotFoundError:
        result = {"state": "gone"} if not path.exists() else {"state": "needs_attention", "error": "Отсутствует файл приватного пакета."}
    except (RecoveryConflict, CardError, ValidationError, ValueError, TypeError) as exc:
        result = {"state": "needs_attention", "error": str(exc)}
        logger.error("Browser upload recovery needs attention at %s: %s", path, exc)
    except (SQLAlchemyError, OSError) as exc:
        result = {"state": "deferred", "error": "NAS или локальная файловая система временно недоступны."}
        logger.warning("Browser upload recovery deferred at %s: %s", path, type(exc).__name__)
    if receipt is not None:
        try:
            _save_state(path, receipt, result, now=now, settings=settings)
        except (OSError, ValueError, TypeError):
            logger.warning("Could not save recovery state at %s; immutable job ID makes the next attempt safe", path)
    return result


def recover_upload_receipts(*, settings: Settings, limit: int = 10) -> list[dict]:
    """Bounded non-recursive scan; another PC's paths are never followed."""
    incoming = Path(settings.raw_logs_container_path).resolve() / "incoming"
    if not incoming.is_dir() or incoming.is_symlink():
        return []
    results = []
    for path in incoming.iterdir():
        if path.is_symlink() or not path.is_file() or not path.name.startswith(BROWSER_UPLOAD_PREFIX) or path.suffix != ".json":
            continue
        result = recover_upload_receipt(path, settings=settings)
        if result["state"] == "skipped":
            continue
        results.append(result)
        if result["state"] == "deferred" or len(results) >= limit:
            break  # Avoid multiplying a NAS outage by every pending receipt.
    return results
