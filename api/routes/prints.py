"""Print archive endpoints: print record CRUD, search and file attachments."""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import shutil
from datetime import datetime, time, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile

from api.deps.repositories import get_prints_repository
from api.upload_limits import read_upload_capped
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
# Scanning fields that a material preset overrides in machine_params
_PRESET_SCANNING_KEYS = (
    "hatch_speed_mm_s", "contour_speed_mm_s", "hatch_distance_mm",
    "layer_thickness_mm", "jump_speed_mm_s", "jump_delay_ms",
)


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
    preset_materials = sorted({p["material"] for p in repo.list_presets()})
    density_materials = sorted((params.get("material_densities") or {}).keys())
    materials = preset_materials or density_materials or _DEFAULT_MATERIALS
    return {
        "powder_cost_rub_per_kg": repo.last_powder_cost(),
        "materials": materials,
    }


@router.get("/prediction-accuracy")
def get_prediction_accuracy(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Predicted vs actual report + per-material suggested correction factors."""
    from analytics.prediction.accuracy import prediction_accuracy

    return prediction_accuracy(repo.db)


@router.post("/recalibrate")
def recalibrate(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Recompute and apply per-material time-correction factors from history.

    No-op when factors are pinned manually (correction_locked).
    """
    from analytics.prediction.accuracy import recalibrate_and_apply

    result = recalibrate_and_apply(repo.db)
    repo.flush()
    return result


def _combined_prediction(
    blobs: list[bytes],
    material: str,
    params: dict,
    powder_cost: float | None,
) -> dict:
    """Time + cost estimate over a full print platform (parts + supports STLs).

    This is how a Magics layout is estimated: the operator exports the whole
    plate (every part, its copies and supports, in the real build orientation)
    as STLs and we slice them together as one build.

    Semantics:
    - scan  = Σ per-part scan times  (parts scanned independently by the laser)
    - recoat = from the tallest part (one recoat pass per layer for the whole platform)
    - volume / powder = Σ parts
    - correction = uniform per material, so combined raw = combined print / factor
    """
    from analytics.prediction.cost_estimator import estimate_cost
    from analytics.prediction.print_time import EstimationError, PrintTimeEstimate, estimate_print_time
    from analytics.prediction.stl_slicer import SliceResult, slice_stl

    try:
        layer_thickness = float(params["layer_thickness_mm"])
        per_part = [
            (slc := slice_stl(blob, layer_thickness),
             estimate_print_time(slc, params, material, stl_bytes=blob))
            for blob in blobs
        ]

        scan_hours = sum(te.scan_hours for _, te in per_part)
        # Recoat is a single pass per layer for the tallest part on the platform
        tallest = max(range(len(per_part)), key=lambda i: per_part[i][0].layer_count)
        recoat_hours = per_part[tallest][1].recoat_hours
        print_hours = scan_hours + recoat_hours

        factor = per_part[0][1].correction_factor or 1.0
        raw_print_hours = print_hours / factor if factor else print_hours

        # Deduplicated warnings from all parts
        seen: set[str] = set()
        warnings: list[str] = []
        for _, te in per_part:
            for w in te.warnings:
                if w not in seen:
                    seen.add(w)
                    warnings.append(w)

        combined_time = PrintTimeEstimate(
            scan_hours=scan_hours,
            recoat_hours=recoat_hours,
            print_hours=print_hours,
            total_days=print_hours / 24.0,
            method=per_part[0][1].method,
            raw_print_hours=raw_print_hours,
            correction_factor=factor,
            warnings=warnings,
        )
        combined_slices = SliceResult(
            volume_mm3=sum(sl.volume_mm3 for sl, _ in per_part),
            height_mm=max(sl.height_mm for sl, _ in per_part),
            layer_count=max(sl.layer_count for sl, _ in per_part),
            layer_thickness_mm=layer_thickness,
        )
        cost_est = estimate_cost(combined_slices, params, material, combined_time,
                                 powder_cost_override=powder_cost)
    except EstimationError as exc:
        return {"available": False, "reason": str(exc)}
    except Exception:
        logger.exception("prints: combined prediction failed")
        return {"available": False, "reason": "Не удалось нарезать модель — проверьте файлы"}

    return {
        "available": True,
        "n_parts": len(per_part),
        "method": combined_time.method,
        "build_axis": "Z",
        "layer_count": combined_slices.layer_count,
        "height_mm": round(combined_slices.height_mm, 2),
        "print_hours": round(print_hours, 3),
        "raw_print_hours": round(raw_print_hours, 3),
        "correction_factor": round(factor, 3),
        "scan_hours": round(scan_hours, 3),
        "recoat_hours": round(recoat_hours, 3),
        "cost_total_rub": cost_est.total_rub,
        "warnings": combined_time.warnings + cost_est.warnings,
    }


def _compute_prediction_snapshot(repo: PrintsRepository, record_id: str) -> dict:
    """Run the PySLM time/cost estimate over the whole platform and store the snapshot.

    The platform = every attached part STL **plus its support STLs** (file types
    "stl" and "stl_supports"), so supports are counted in the burn volume. This
    is the path used for a full Magics layout exported to STL.

    Raises HTTPException with the reason when the estimate cannot run.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")

    files = repo.list_print_files(record_id)
    platform_files = [f for f in files if f["file_type"] in ("stl", "stl_supports")]
    if not platform_files:
        raise HTTPException(422, "К карточке не прикреплён STL")

    from api.routes.machine_settings import params_configured

    material = record["material"]
    params = repo.get_machine_params()
    preset = repo.get_active_preset_for_material(material)
    if preset:
        params = {**(params or {}), **{k: v for k, v in preset.items() if k in _PRESET_SCANNING_KEYS and v is not None}}
    if not params_configured(params):
        raise HTTPException(
            422, "Заполните параметры машины (вкладка Настройки → Машина) перед расчётом"
        )

    store = ObjectStore()
    blobs: list[bytes] = []
    n_supports = 0
    for f in platform_files:
        bucket, _, object_name = f["object_uri"].removeprefix("s3://").partition("/")
        data = store.get_bytes(bucket, object_name)
        if data is None:
            raise HTTPException(503, f"STL недоступен в хранилище: {f['file_name']}")
        blobs.append(data)
        if f["file_type"] == "stl_supports":
            n_supports += 1

    powder_cost = record.get("powder_cost_rub_per_kg") or repo.last_powder_cost()
    result = _combined_prediction(blobs, material, params, powder_cost)
    if not result.get("available"):
        raise HTTPException(422, f"Расчёт недоступен: {result.get('reason')}")

    snapshot: dict = {
        "estimated_at": datetime.now(timezone.utc).isoformat(),
        "n_parts": len(blobs),
        "n_supports": n_supports,
        "material": material,
        "method": result["method"],
        "build_axis": result.get("build_axis", "Z"),
        "layer_count": result.get("layer_count"),
        "print_hours": result["print_hours"],
        # raw (uncorrected) hours feed the calibration loop, so the learned
        # factor stays absolute and never compounds on itself.
        "raw_print_hours": result.get("raw_print_hours", result["print_hours"]),
        "correction_factor": result.get("correction_factor", 1.0),
        "cost_total_rub": result["cost_total_rub"],
    }

    meta = dict(record.get("metadata_json") or {})
    meta["prediction"] = snapshot
    repo.update_print_record(record_id, {"metadata_json": meta})
    repo.flush()
    logger.info(
        "prints: prediction stored for %s (%d parts incl %d supports, %.1fh, ×%.3f)",
        record_id, len(blobs), n_supports, snapshot["print_hours"], snapshot["correction_factor"],
    )
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
        new_session_id = payload["session_id"] or None
        values["session_id"] = new_session_id
        if new_session_id and "printed_at" not in payload:
            from domain.models.sessions import BuildSession
            session = repo.db.get(BuildSession, new_session_id)
            if session and session.start_ts:
                ts = session.start_ts
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                values["printed_at"] = ts
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
    # Manually linking a record to a session creates a new predicted/actual pair
    # → refresh per-material time-correction factors.
    if values.get("session_id"):
        from analytics.prediction.accuracy import recalibrate_and_apply
        try:
            recalibrate_and_apply(repo.db)
            repo.flush()
        except Exception:
            logger.exception("auto-calibration after manual link failed")
    return record


@router.delete("/{record_id}")
def delete_print(
    record_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Delete a record with all attached files (DB rows + stored objects)."""
    if not repo.get_print_record(record_id):
        raise HTTPException(404, "Карточка печати не найдена")
    uris = repo.delete_print_record(record_id)
    repo.flush()
    # MinIO cleanup runs after get_db commits so DB and object store stay in sync
    background_tasks.add_task(_remove_objects, uris)
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

    data = await read_upload_capped(file, _MAX_UPLOAD_MB * 1024 * 1024)
    if not data:
        raise HTTPException(422, "Пустой файл")

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
    if file_type == "stl":
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
        total = 0
        target = dest / name
        too_big = False
        tmp_path = f"/tmp/{os.urandom(8).hex()}.upload"
        try:
            with open(tmp_path, "wb") as buf:
                while chunk := await f.read(16 * 1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_FILE_MB * 1024 * 1024:
                        too_big = True
                        break
                    buf.write(chunk)
            if too_big:
                os.unlink(tmp_path)
                skipped.append({"name": name, "reason": f"файл > {_MAX_FILE_MB} МБ"})
            else:
                shutil.move(tmp_path, target)
                saved.append({"name": name, "size_bytes": total})
                printed_at_hint = printed_at_hint or _date_from_text(name)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

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
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Detach one file from a record (DB row + stored object)."""
    uri = repo.delete_print_file(record_id, file_id)
    if uri is None:
        raise HTTPException(404, "Файл не найден")
    repo.flush()
    # MinIO cleanup runs after get_db commits so DB and object store stay in sync
    background_tasks.add_task(_remove_objects, [uri])
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
