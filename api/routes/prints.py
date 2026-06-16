"""Print archive endpoints: print record CRUD, search and file attachments."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from datetime import datetime, time, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile

from api.deps.repositories import get_prints_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from core.config.settings import get_settings
from parsers.common.timestamps import date_hint_from_filename
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prints", tags=["prints"])

_STATUSES = {"draft", "active", "completed"}
_FILE_TYPES = {"stl", "stl_supports", "magics", "photo", "doc"}
_MAX_UPLOAD_MB = 600
# Materials offered when machine_params has no densities configured yet
_DEFAULT_MATERIALS = ["steel", "aluminum", "titanium", "other"]


def _bucket_for(file_type: str) -> str:
    settings = get_settings()
    return {
        "stl": settings.minio_bucket_stls,
        "stl_supports": settings.minio_bucket_stls,
        "magics": settings.minio_bucket_magics,
        "photo": settings.minio_bucket_photos,
        "doc": settings.minio_bucket_docs,
    }[file_type]


def _clean_material(raw: str | None) -> str:
    material = (raw or "").strip().lower()
    if not material:
        raise HTTPException(422, "Поле 'material' не может быть пустым")
    if len(material) > 120:
        raise HTTPException(422, "Поле 'material' слишком длинное (макс. 120)")
    return material


def _parse_iso_datetime(raw, field: str) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        raise HTTPException(422, f"Поле '{field}' должно быть датой ISO (ГГГГ-ММ-ДД)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_powder_cost(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Поле 'powder_cost_rub_per_kg' должно быть числом")
    if value < 0:
        raise HTTPException(422, "Цена порошка не может быть отрицательной")
    return value


def _date_from_text(text: str) -> datetime | None:
    """Print date hint from a record/file name like '23.03.2026_кронштейн'."""
    hint = date_hint_from_filename(Path(text))
    return datetime.combine(hint, time(), tzinfo=timezone.utc) if hint else None


@router.post("")
def create_print(payload: dict, repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Create a print record.

    Body: {name, material?, notes?, printed_at?, powder_cost_rub_per_kg?}.
    When printed_at is omitted, a date embedded in the name is used if found;
    the linked log session overwrites it later with the real start time.
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Поле 'name' обязательно")
    material = _clean_material(payload.get("material") or "steel")
    printed_at = _parse_iso_datetime(payload.get("printed_at"), "printed_at") or _date_from_text(name)

    record = repo.create_print_record({
        "name": name,
        "material": material,
        "notes": (payload.get("notes") or "").strip() or None,
        "printed_at": printed_at,
        "powder_cost_rub_per_kg": _parse_powder_cost(payload.get("powder_cost_rub_per_kg")),
    })
    repo.flush()
    logger.info("prints: created %s (%s)", record["record_id"], name)
    return record


@router.get("")
def list_prints(
    skip: SkipParam = 0,
    limit: LimitParam = 50,
    q: str | None = None,
    material: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Paginated list, newest print date first. Filters: q (name), material, date range."""
    filters = {
        "query": (q or "").strip() or None,
        "material": (material or "").strip().lower() or None,
        "date_from": _parse_iso_datetime(date_from, "date_from"),
        "date_to": _parse_iso_datetime(date_to, "date_to"),
    }
    records = repo.list_print_records(skip=skip, limit=limit, **filters)
    for record in records:
        record["files"] = repo.list_print_files(record["record_id"])
    total = repo.count_print_records(**filters)
    return PaginatedResponse(items=records, total=total, skip=skip, limit=limit).to_dict()


@router.get("/defaults")
def print_defaults(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Prefill values for the new-print form: last powder price + known materials."""
    params = repo.get_machine_params() or {}
    materials = sorted((params.get("material_densities") or {}).keys()) or _DEFAULT_MATERIALS
    return {
        "powder_cost_rub_per_kg": repo.last_powder_cost(),
        "materials": materials,
    }


@router.get("/prediction-accuracy")
def get_prediction_accuracy(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Predicted vs actual report + suggested time_correction_factor."""
    from analytics.prediction.accuracy import prediction_accuracy

    return prediction_accuracy(repo.db)


def _compute_prediction_snapshot(repo: PrintsRepository, record_id: str) -> dict:
    """Run both time/cost estimates on the record's STL and store the snapshot.

    Raises HTTPException with the reason when the estimate cannot run.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    stl_file = next(
        (f for f in repo.list_print_files(record_id) if f["file_type"] == "stl"), None,
    )
    if not stl_file:
        raise HTTPException(422, "К карточке не прикреплён STL (без поддержек)")

    bucket, _, object_name = stl_file["object_uri"].removeprefix("s3://").partition("/")
    data = ObjectStore().get_bytes(bucket, object_name)
    if data is None:
        raise HTTPException(503, "STL недоступен в хранилище")

    from api.routes.uploads import _geometry_prediction

    material = record["material"]
    snapshot: dict = {"estimated_at": datetime.now(timezone.utc).isoformat()}
    for key, mode in (("fast", "excel"), ("accurate", "pyslm")):
        result = _geometry_prediction(data, material, mode=mode)
        if not result.get("available"):
            raise HTTPException(422, f"Расчёт недоступен: {result.get('reason')}")
        snapshot[key] = {
            "method": result["method"],
            "print_hours": result["print_hours"],
            "cost_total_rub": result["cost_total_rub"],
        }

    meta = dict(record.get("metadata_json") or {})
    meta["prediction"] = snapshot
    repo.update_print_record(record_id, {"metadata_json": meta})
    repo.flush()
    logger.info("prints: prediction stored for %s (fast=%.1fh, accurate=%.1fh)",
                record_id, snapshot["fast"]["print_hours"], snapshot["accurate"]["print_hours"])
    return snapshot


def _auto_estimate(record_id: str) -> None:
    """Background prediction after an STL upload — best-effort, own DB session."""
    from storage.db.session import session_scope

    try:
        with session_scope() as db:
            repo = PrintsRepository(db)
            _compute_prediction_snapshot(repo, record_id)
    except HTTPException as exc:
        # Параметры машины не заполнены и т.п. — это не ошибка загрузки файла
        logger.info("prints: auto-estimate for %s skipped: %s", record_id, exc.detail)
    except Exception:
        logger.exception("prints: auto-estimate for %s failed", record_id)


@router.post("/{record_id}/estimate")
def estimate_print_record(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Manual (re)run of the prediction snapshot for a record's STL."""
    snapshot = _compute_prediction_snapshot(repo, record_id)
    return {"record_id": record_id, "prediction": snapshot}


@router.get("/{record_id}")
def get_print(record_id: str, repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Full print record with attached files."""
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    record["files"] = repo.list_print_files(record_id)
    return record


@router.patch("/{record_id}")
def update_print(
    record_id: str,
    payload: dict,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Partial update: name, material, notes, status, session_id, printed_at, powder cost."""
    values: dict = {}
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise HTTPException(422, "Поле 'name' не может быть пустым")
        values["name"] = name
    if "material" in payload:
        values["material"] = _clean_material(payload["material"])
    if "status" in payload:
        status = (payload["status"] or "").strip().lower()
        if status not in _STATUSES:
            raise HTTPException(422, f"Недопустимый статус. Допустимы: {', '.join(sorted(_STATUSES))}")
        values["status"] = status
    if "notes" in payload:
        values["notes"] = (payload["notes"] or "").strip() or None
    if "session_id" in payload:
        values["session_id"] = payload["session_id"] or None
    if "printed_at" in payload:
        values["printed_at"] = _parse_iso_datetime(payload["printed_at"], "printed_at")
    if "powder_cost_rub_per_kg" in payload:
        values["powder_cost_rub_per_kg"] = _parse_powder_cost(payload["powder_cost_rub_per_kg"])
    if not values:
        raise HTTPException(422, "Нет полей для обновления")

    record = repo.update_print_record(record_id, values)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    repo.flush()
    return record


@router.delete("/{record_id}")
def delete_print(record_id: str, repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Delete a record with all attached files (DB rows + stored objects)."""
    if not repo.get_print_record(record_id):
        raise HTTPException(404, "Карточка печати не найдена")
    uris = repo.delete_print_record(record_id)
    repo.flush()
    _remove_objects(uris)
    logger.info("prints: deleted %s (%d files)", record_id, len(uris))
    return {"deleted": record_id, "files_removed": len(uris)}


def _remove_objects(uris: list[str]) -> None:
    """Best-effort MinIO cleanup after the DB rows are gone."""
    if not uris:
        return
    store = ObjectStore()
    for uri in uris:
        bucket, _, object_name = uri.removeprefix("s3://").partition("/")
        if not store.remove_object(bucket, object_name):
            logger.warning("prints: could not remove %s from storage", uri)


@router.post("/{record_id}/files")
async def upload_print_file(
    record_id: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    file_type: str = Form(...),
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Attach a file (STL / Magics / photo / doc) to a print record.

    The object key includes the content checksum, so same-named files with
    different content never overwrite each other; identical uploads dedupe.
    Uploading a part STL schedules the time/cost prediction in the background.
    """
    if file_type not in _FILE_TYPES:
        raise HTTPException(422, f"Недопустимый file_type. Допустимы: {', '.join(sorted(_FILE_TYPES))}")
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")

    data = await file.read()
    if not data:
        raise HTTPException(422, "Пустой файл")
    if len(data) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Файл > {_MAX_UPLOAD_MB} МБ")

    file_name = (file.filename or "unknown").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # MagicsX support exports use the s_ prefix — classify them automatically
    if file_type == "stl" and file_name.lower().startswith("s_"):
        file_type = "stl_supports"

    checksum = hashlib.sha256(data).hexdigest()
    existing = repo.find_file_by_checksum(record_id, checksum)
    if existing:
        return {"duplicate": True, **existing}

    store = ObjectStore()
    if not store.is_available():
        raise HTTPException(503, "Хранилище файлов (MinIO) недоступно")
    content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    object_uri = store.put_bytes(
        _bucket_for(file_type), f"{record_id}/{checksum[:8]}_{file_name}", data,
        content_type=content_type,
    )

    saved = repo.add_print_file({
        "record_id": record_id,
        "object_uri": object_uri,
        "file_name": file_name,
        "file_type": file_type,
        "size_bytes": len(data),
        "checksum": checksum,
    })
    # A dated file name pins down the print date when the record has none yet
    if not record.get("printed_at"):
        from_file = _date_from_text(file_name)
        if from_file:
            repo.update_print_record(record_id, {"printed_at": from_file})
    # Commit now (not at the request boundary): the background auto-estimate
    # runs in its own session and must see the just-attached file committed.
    repo.db.commit()
    # Деталь без поддержек → автоматический прогноз времени/стоимости в фоне,
    # чтобы пара «прогноз/факт» образовалась без ручного нажатия 📐
    if file_type == "stl" and not (record.get("metadata_json") or {}).get("prediction"):
        background_tasks.add_task(_auto_estimate, record_id)
    logger.info("prints: attached %s (%s, %d bytes) to %s", file_name, file_type, len(data), record_id)
    return saved


@router.get("/{record_id}/session-candidates")
def get_session_candidates(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Sessions near the record's print date (incl. ambiguous) for manual linking."""
    if not repo.get_print_record(record_id):
        raise HTTPException(404, "Карточка печати не найдена")
    from domain.services.print_linking import session_candidates

    return {"candidates": session_candidates(repo.db, record_id)}


@router.post("/{record_id}/import-logs")
async def import_logs_for_print(
    record_id: str,
    files: list[UploadFile],
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Upload printer logs for a specific print record.

    Files land in the raw-logs folder and go through the standard ingestion
    pipeline; auto-linking by date attaches the created session back to this
    record. A dated log file name fills the record's print date when empty.
    """
    from api.routes.uploads import _ALLOWED_SUFFIXES, _MAX_FILE_MB, _trigger_rescan

    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")

    settings = get_settings()
    dest = Path(settings.raw_logs_container_path)
    if not dest.exists():
        raise HTTPException(500, f"Папка логов не найдена: {dest}")

    saved, skipped = [], []
    printed_at_hint = None
    for f in files:
        name = Path(f.filename or "unknown").name
        if Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
            skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
            continue
        data = await f.read()
        if len(data) > _MAX_FILE_MB * 1024 * 1024:
            skipped.append({"name": name, "reason": f"файл > {_MAX_FILE_MB} МБ"})
            continue
        (dest / name).write_bytes(data)
        saved.append({"name": name, "size_bytes": len(data)})
        printed_at_hint = printed_at_hint or _date_from_text(name)

    updates: dict = {}
    if not record.get("printed_at") and printed_at_hint:
        updates["printed_at"] = printed_at_hint
    if printed_at_hint:
        # Explicit intent: these logs belong to THIS record — the auto-linker
        # resolves the hint even when another record matches the same date.
        meta = dict(record.get("metadata_json") or {})
        meta["log_import_hint"] = {"date": printed_at_hint.date().isoformat()}
        updates["metadata_json"] = meta
    if updates:
        repo.update_print_record(record_id, updates)
        # Durable now: the background rescan/auto-link reads this in its own session.
        repo.db.commit()

    if saved:
        _trigger_rescan(settings.raw_logs_container_path)
    logger.info("prints: %d log file(s) uploaded for %s", len(saved), record_id)
    return {"saved": saved, "skipped": skipped,
            "note": "Логи импортируются в фоне; сессия привяжется к карточке по дате печати."}


@router.delete("/{record_id}/files/{file_id}")
def delete_print_file(
    record_id: str,
    file_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Detach one file from a record (DB row + stored object)."""
    uri = repo.delete_print_file(record_id, file_id)
    if uri is None:
        raise HTTPException(404, "Файл не найден")
    repo.flush()
    _remove_objects([uri])
    return {"deleted": file_id}


@router.get("/{record_id}/files/{file_id}/download")
def download_print_file(
    record_id: str,
    file_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
):
    """Stream a stored file back (used by the dashboard STL viewer)."""
    from fastapi.responses import Response

    files = repo.list_print_files(record_id)
    match = next((f for f in files if f["file_id"] == file_id), None)
    if not match:
        raise HTTPException(404, "Файл не найден")

    uri = match["object_uri"]  # s3://bucket/object_name
    bucket, _, object_name = uri.removeprefix("s3://").partition("/")
    data = ObjectStore().get_bytes(bucket, object_name)
    if data is None:
        raise HTTPException(503, "Файл недоступен в хранилище")
    content_type = mimetypes.guess_type(match["file_name"])[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{match["file_name"]}"'},
    )
