"""Print archive endpoints: print record CRUD, search and file attachments."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from sqlalchemy import select

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


def _content_disposition(file_name: str) -> str:
    """attachment header for ``file_name``, safe to place in an HTTP header.

    Uploaded names reach here unchanged apart from path stripping, so a quote or
    newline in one would break out of the quoted-string (or the header itself).
    RFC 5987's filename* carries the real, non-ASCII-capable name; the quoted
    fallback is sanitised for clients that ignore it.
    """
    from urllib.parse import quote

    ascii_fallback = "".join(c for c in file_name if c.isprintable() and c not in '"\\;\r\n')
    ascii_fallback = ascii_fallback.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"


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


def _parse_layer_thickness(raw) -> float | None:
    """Layer thickness in mm, or None for "use the machine default"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Поле 'layer_thickness_mm' должно быть числом")
    # Loose bounds: SLM layers run roughly 0.02–0.1 mm, but the guard only has
    # to reject nonsense (a value in microns, a negative) — the exact process
    # window is the operator's call, not this endpoint's.
    if not (0.0 < value <= 1.0):
        raise HTTPException(422, "Толщина слоя должна быть в мм, в диапазоне 0–1")
    return value


def _parse_hatch_distance(raw) -> float | None:
    """Hatch distance in mm, or None for "use the material preset"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Поле 'hatch_distance_mm' должно быть числом")
    # This machine's own logs show applied values from 0.10 to 0.90 mm, and the
    # operator briefly typed 3.00 while editing, so the window is genuinely
    # wide. The guard only rejects nonsense (microns, a negative).
    if not (0.0 < value <= 5.0):
        raise HTTPException(422, "Шаг штриховки должен быть в мм, в диапазоне 0–5")
    return value


def _date_from_text(text: str) -> datetime | None:
    """Print date hint from a record/file name like '23.03.2026_кронштейн'."""
    hint = date_hint_from_filename(Path(text))
    return datetime.combine(hint, time(), tzinfo=timezone.utc) if hint else None


@router.post("")
def create_print(payload: dict, repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Create a print record.

    Body: {name, material?, layer_thickness_mm?, hatch_distance_mm?, notes?,
    printed_at?, powder_cost_rub_per_kg?}. When printed_at is omitted, a date
    embedded in the name is used if found; the linked log session overwrites it
    later with the real start time.
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Поле 'name' обязательно")
    material = _clean_material(payload.get("material") or "steel")
    printed_at = _parse_iso_datetime(payload.get("printed_at"), "printed_at") or _date_from_text(name)

    record = repo.create_print_record({
        "name": name,
        "material": material,
        "layer_thickness_mm": _parse_layer_thickness(payload.get("layer_thickness_mm")),
        "hatch_distance_mm": _parse_hatch_distance(payload.get("hatch_distance_mm")),
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
    files_by_record = repo.list_files_for_records([r["record_id"] for r in records])
    for record in records:
        record["files"] = files_by_record.get(record["record_id"], [])
    _attach_plan_vs_fact(repo, records)
    total = repo.count_print_records(**filters)
    return PaginatedResponse(items=records, total=total, skip=skip, limit=limit).to_dict()


def _attach_plan_vs_fact(repo: PrintsRepository, records: list[dict]) -> None:
    """Add a ``summary`` to each record: what was predicted, what happened, the gap.

    The list is where the operator compares the two, so both have to arrive in
    one response — the prediction lives in the record's own snapshot while the
    outcome lives on the linked session, and fetching them separately per row
    would be a query per print.

    The actual is machine time (scan + recoat) whenever the printer's own
    time_log covers the session, never the wall-clock span: the estimate models
    machine time only, so comparing it against a span that includes operator
    pauses reports an error the geometry never made. On one real build that gap
    was 18 of 47.6 hours.
    """
    from domain.models.sessions import BuildSession

    session_ids = [r["session_id"] for r in records if r.get("session_id")]
    sessions: dict[str, BuildSession] = {}
    if session_ids:
        sessions = {
            s.session_id: s for s in repo.db.scalars(
                select(BuildSession).where(BuildSession.session_id.in_(session_ids))
            ).all()
        }

    for record in records:
        snapshot = (record.get("metadata_json") or {}).get("prediction") or {}
        predicted_hours = snapshot.get("print_hours")

        actual_hours = actual_source = idle_hours = layers = None
        session = sessions.get(record.get("session_id") or "")
        if session is not None:
            features = (
                ((session.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
            ).get("features") or {}
            layers = features.get("layers")
            if features.get("idle_min") is not None:
                idle_hours = round(features["idle_min"] / 60, 2)
            if features.get("machine_min"):
                actual_hours, actual_source = round(features["machine_min"] / 60, 2), "machine_log"
            elif features.get("duration_min"):
                actual_hours, actual_source = round(features["duration_min"] / 60, 2), "wall_span"

        error_pct = None
        if predicted_hours and actual_hours:
            error_pct = round((predicted_hours - actual_hours) / actual_hours * 100, 1)

        record["summary"] = {
            "predicted_hours": round(predicted_hours, 2) if predicted_hours else None,
            "predicted_cost_rub": snapshot.get("cost_total_rub"),
            "actual_hours": actual_hours,
            "actual_source": actual_source,
            "idle_hours": idle_hours,
            "layers": layers,
            "error_pct": error_pct,
        }


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


@router.get("/unlinked-sessions")
def get_unlinked_sessions(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Log sessions belonging to no print card — surfaced as unfinished work.

    Until a session is linked it contributes nothing: no cost, no
    predicted-vs-actual pair, no calibration input.
    """
    from domain.services.print_linking import unlinked_sessions

    sessions = unlinked_sessions(repo.db)
    return {
        "items": sessions,
        "total": len(sessions),
        "n_prints": sum(1 for s in sessions if s["is_print"]),
    }


@router.get("/prediction-accuracy")
def get_prediction_accuracy(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Predicted vs actual report: scan-time correction factors + recoat time,
    both learned per material from history."""
    from analytics.prediction.accuracy import prediction_accuracy
    from analytics.prediction.recoat_calibration import recoat_accuracy
    from analytics.prediction.scan_calibration import scan_calibration_report

    report = prediction_accuracy(repo.db)
    report["recoat"] = recoat_accuracy(repo.db)
    report["scan"] = scan_calibration_report(repo.db)
    return report


@router.post("/recalibrate")
def recalibrate(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Recompute and apply per-material time-correction factors and recoat time
    from history.

    No-op when pinned manually (correction_locked) — one lock for both.
    """
    from analytics.prediction.accuracy import recalibrate_and_apply
    from analytics.prediction.recoat_calibration import recalibrate_recoat_and_apply
    from analytics.prediction.scan_calibration import recalibrate_scan_and_apply

    result = recalibrate_and_apply(repo.db)
    result["recoat"] = recalibrate_recoat_and_apply(repo.db)
    result["scan"] = recalibrate_scan_and_apply(repo.db)
    repo.flush()
    return result


def _combined_prediction(
    parts: list[tuple[str, bytes]],
    supports: list[tuple[str, bytes]],
    material: str,
    params: dict,
    powder_cost: float | None,
    geometry_cache: Any | None = None,
) -> dict:
    """Time + cost estimate over a full print platform (parts + supports STLs).

    All bodies are co-hatched per plate layer by the layer engine (real vectors,
    shared Z axis) — see analytics.prediction.plate_estimator / layer_engine.

    Powder mass for the cost estimate uses part volume only: sheet supports
    have no meaningful mesh volume (flagged in the response warnings).

    ``geometry_cache`` (typically the calling repo) skips re-slicing when an
    identical set of STL bodies at the same hatch_distance_mm was estimated
    before — see plate_estimator._geometry_cache_key.
    """
    from analytics.prediction.cost_estimator import estimate_cost
    from analytics.prediction.plate_estimator import estimate_plate
    from analytics.prediction.stl_slicer import EstimationError, SliceResult

    try:
        est = estimate_plate(parts, supports, params, material, geometry_cache=geometry_cache)

        combined_slices = SliceResult(
            volume_mm3=est.parts_volume_mm3,
            height_mm=est.height_mm,
            layer_count=est.layer_count,
            layer_thickness_mm=float(params["layer_thickness_mm"]),
        )
        cost_est = estimate_cost(combined_slices, params, material,
                                 est.as_print_time_estimate(),
                                 powder_cost_override=powder_cost)
        cost_warnings = list(cost_est.warnings)
        if supports:
            cost_warnings.append(
                "Масса порошка поддержек не входит в стоимость (объём листовых поддержек не определён)."
            )
    except EstimationError as exc:
        return {"available": False, "reason": str(exc)}
    except Exception:
        logger.exception("prints: combined prediction failed")
        return {"available": False, "reason": "Не удалось нарезать модель — проверьте файлы"}

    return {
        "available": True,
        "n_parts": sum(1 for b in est.bodies if b.kind == "part"),
        "n_support_bodies": sum(1 for b in est.bodies if b.kind == "support"),
        "method": est.method,
        "build_axis": "Z",
        "layer_count": est.layer_count,
        "height_mm": round(est.height_mm, 2),
        "print_hours": round(est.print_hours, 3),
        "raw_print_hours": round(est.raw_print_hours, 3),
        "correction_factor": round(est.correction_factor, 3),
        "scan_hours": round(est.scan_hours, 3),
        "recoat_hours": round(est.recoat_hours, 3),
        "cost_total_rub": cost_est.total_rub,
        "scan_source": est.scan_source,
        "warnings": est.warnings + cost_warnings,
        # Per-layer geometry series — persisted into the snapshot so scan
        # calibration can later pair it with real burn_ms without re-slicing.
        "scan_geometry": {
            **est.geometry_series.to_snapshot(),
            "layer_thickness_mm": float(params["layer_thickness_mm"]),
            "laser_count": int(params.get("laser_count") or 1),
        } if est.geometry_series is not None else None,
    }


def params_for_record(repo: PrintsRepository, record: dict) -> dict:
    """Scanning parameters for one print, most specific source winning.

    machine_params (global) < material preset < the print's own fields.

    Both per-print overrides exist because this shop changes them per job while
    the machine holds one global value:

    * ``layer_thickness_mm`` also selects which fitted scan model applies —
      those are keyed "material@thickness" and do not transfer across
      thicknesses.
    * ``hatch_distance_mm`` rescales the whole estimate, since scan length goes
      as ~1/hatch. The material preset claimed a fixed 0.12 mm while the
      machine's Monitor100 log recorded 0.16 / 0.10 / 0.90 mm applied on steel
      jobs — a 9x span that landed entirely in the prediction error.

    NULL on the record means "not specified": fall through to the preset, then
    to the machine default, so records that predate these fields are unaffected.
    """
    params = dict(repo.get_machine_params() or {})
    preset = repo.get_active_preset_for_material(record["material"])
    if preset:
        params.update({
            k: v for k, v in preset.items()
            if k in _PRESET_SCANNING_KEYS and v is not None
        })
    for field in ("layer_thickness_mm", "hatch_distance_mm"):
        if record.get(field):
            params[field] = record[field]
    return params


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

    from api.routes.machine_settings import effective_params, missing_for_estimation

    material = record["material"]
    params = params_for_record(repo, record)
    missing = missing_for_estimation(params)
    if missing:
        raise HTTPException(
            422,
            "Для расчёта не хватает параметров машины: "
            + ", ".join(missing)
            + ". Заполните их в Настройки → Параметры машины.",
        )
    params = effective_params(params)

    store = ObjectStore()
    parts: list[tuple[str, bytes]] = []
    supports: list[tuple[str, bytes]] = []
    for f in platform_files:
        bucket, _, object_name = f["object_uri"].removeprefix("s3://").partition("/")
        data = store.get_bytes(bucket, object_name)
        if data is None:
            raise HTTPException(503, f"STL недоступен в хранилище: {f['file_name']}")
        if f["file_type"] == "stl_supports":
            supports.append((f["file_name"], data))
        else:
            parts.append((f["file_name"], data))
    n_supports = len(supports)

    powder_cost = record.get("powder_cost_rub_per_kg") or repo.last_powder_cost()
    result = _combined_prediction(parts, supports, material, params, powder_cost, geometry_cache=repo)
    if not result.get("available"):
        raise HTTPException(422, f"Расчёт недоступен: {result.get('reason')}")

    snapshot: dict = {
        "estimated_at": datetime.now(timezone.utc).isoformat(),
        "n_parts": len(parts),
        "n_supports": n_supports,
        "material": material,
        # The two geometry inputs that scale the whole estimate. Recorded so a
        # stored prediction can be read back and checked against the machine
        # log — without them there is no way to tell what a number was computed
        # at, which is exactly how a preset's 0.12 mm went unnoticed against
        # 0.90 mm on the machine.
        "layer_thickness_mm": params.get("layer_thickness_mm"),
        "hatch_distance_mm": params.get("hatch_distance_mm"),
        "method": result["method"],
        "build_axis": result.get("build_axis", "Z"),
        "layer_count": result.get("layer_count"),
        "print_hours": result["print_hours"],
        # raw (uncorrected) hours feed the calibration loop, so the learned
        # factor stays absolute and never compounds on itself.
        "raw_print_hours": result.get("raw_print_hours", result["print_hours"]),
        "correction_factor": result.get("correction_factor", 1.0),
        "scan_source": result.get("scan_source", "physics"),
        "cost_total_rub": result["cost_total_rub"],
        "scan_geometry": result.get("scan_geometry"),
    }

    meta = dict(record.get("metadata_json") or {})
    meta["prediction"] = snapshot
    repo.update_print_record(record_id, {"metadata_json": meta})
    repo.flush()
    logger.info(
        "prints: prediction stored for %s (%d parts + %d supports, %.1fh, ×%.3f)",
        record_id, len(parts), n_supports, snapshot["print_hours"], snapshot["correction_factor"],
    )
    return snapshot


def _assert_estimatable(repo: PrintsRepository, record: dict) -> None:
    """Raise if the estimate cannot run, before committing to a long job.

    Only the cheap preconditions — an attached STL and the machine parameters.
    Running these up front means the operator hears "no STL attached" straight
    away instead of watching a background job produce nothing.
    """
    from api.routes.machine_settings import missing_for_estimation

    files = repo.list_print_files(record["record_id"])
    if not [f for f in files if f["file_type"] in ("stl", "stl_supports")]:
        raise HTTPException(422, "К карточке не прикреплён STL")

    # Same resolution the real estimate uses, or this precondition reports a
    # parameter as missing that the record itself supplies.
    missing = missing_for_estimation(params_for_record(repo, record))
    if missing:
        raise HTTPException(
            422,
            "Для расчёта не хватает параметров машины: " + ", ".join(missing)
            + ". Заполните их в Настройки → Параметры машины.",
        )


# PLAN_ACCURACY.md 2.4. One worker: compute_layer_series already parallelises
# internally across 4 threads (layer_engine._SECTION_THREADS) up to this
# container's own CPU limit — a second concurrent estimate would only fight
# the first one for the same cores, not add real throughput. A second request
# just queues behind it in the pool rather than racing it.
_ESTIMATE_POOL: ProcessPoolExecutor | None = None


def _estimate_pool() -> ProcessPoolExecutor:
    global _ESTIMATE_POOL
    if _ESTIMATE_POOL is None:
        _ESTIMATE_POOL = ProcessPoolExecutor(max_workers=1)
    return _ESTIMATE_POOL


def _run_estimate_in_process(record_id: str) -> None:
    """The actual estimate, run in a separate OS process (see _auto_estimate).

    Needs its own DB session — nothing from the api process's session or
    request state crosses this boundary. Module-level so ProcessPoolExecutor
    can pickle a reference to it (a closure or bound method can't be).
    """
    from storage.db.session import session_scope

    with session_scope() as db:
        repo = PrintsRepository(db)
        _compute_prediction_snapshot(repo, record_id)


async def _auto_estimate(record_id: str) -> None:
    """Background prediction after an STL upload or manual re-estimate.

    The heavy part runs in a separate OS process, not a thread in this one:
    compute_layer_series is CPU-bound Python/C, and a thread here would still
    contend for THIS process's own GIL with every other request this instance
    is serving. There is no other operator sharing this process to blame —
    each PC runs its own full stack — so "every other request" means this same
    operator's own next dashboard click while they wait for their own
    estimate. A subprocess sidesteps that; the container's CPU limit still
    applies (raised to 4.0 for exactly this — see docker-compose.yml).

    Tests run this inline in the same process instead (APP_ENV=test): a real
    subprocess would not see this process's monkeypatched ObjectStore — the
    in-memory store tests substitute for MinIO exists only in this process's
    memory, and a spawned/forked child does not share it.
    """
    try:
        if get_settings().app_env == "test":
            _run_estimate_in_process(record_id)
        else:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_estimate_pool(), _run_estimate_in_process, record_id)
    except HTTPException as exc:
        # Параметры машины не заполнены и т.п. — это не ошибка загрузки файла
        logger.info("prints: auto-estimate for %s skipped: %s", record_id, exc.detail)
    except Exception:
        logger.exception("prints: auto-estimate for %s failed", record_id)


@router.post("/{record_id}/estimate")
def estimate_print_record(
    record_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Manual (re)run of the prediction snapshot for a record's STL.

    Runs in the background and returns immediately. Co-hatching a real plate is
    not fast — a three-part build with 32 MB of support meshes takes minutes of
    solid CPU, and the upload path already treats the estimate as a background
    job for exactly that reason. Holding the request open for it means a
    browser or proxy timeout decides whether the result is kept.

    The heavy part also runs in its own OS process (see _auto_estimate), so
    the rest of this dashboard stays responsive while it grinds through a
    heavy plate instead of contending for this process's own GIL.

    The caller polls GET /prints/{id} and watches for metadata_json.prediction
    to appear or its estimated_at to move.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    # Fail fast on the cheap preconditions so the operator hears about a
    # missing STL or an unfilled parameter now, not after a silent no-op.
    _assert_estimatable(repo, record)

    background_tasks.add_task(_auto_estimate, record_id)
    return {
        "record_id": record_id,
        "status": "started",
        "previous": (record.get("metadata_json") or {}).get("prediction"),
    }


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
    """Partial update: name, material, layer thickness, hatch distance, notes,
    status, session_id, printed_at, powder cost."""
    values: dict = {}
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise HTTPException(422, "Поле 'name' не может быть пустым")
        values["name"] = name
    if "material" in payload:
        values["material"] = _clean_material(payload["material"])
    if "layer_thickness_mm" in payload:
        values["layer_thickness_mm"] = _parse_layer_thickness(payload["layer_thickness_mm"])
    if "hatch_distance_mm" in payload:
        values["hatch_distance_mm"] = _parse_hatch_distance(payload["hatch_distance_mm"])
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
    # (and, if the session has a time_log, a new recoat measurement) → refresh both.
    if values.get("session_id"):
        from analytics.prediction.accuracy import recalibrate_and_apply
        from analytics.prediction.recoat_calibration import recalibrate_recoat_and_apply
        from analytics.prediction.scan_calibration import recalibrate_scan_and_apply
        try:
            recalibrate_and_apply(repo.db)
            recalibrate_recoat_and_apply(repo.db)
            recalibrate_scan_and_apply(repo.db)
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
    Uploading a part or support STL schedules the time/cost prediction in the
    background — both, not just parts: a part-only re-estimate silently
    undercounts scan time for any record whose supports haven't been uploaded
    yet at that moment (real supports can be a large share of scan time).
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
    # Деталь или поддержка → автоматический прогноз времени/стоимости в фоне,
    # чтобы пара «прогноз/факт» образовалась без ручного нажатия. Обе ветки —
    # если бы триггерилось только на "stl", загрузка поддержек уже после
    # деталей (обычный порядок ручного и массового прикрепления) молча
    # оставляла бы прогноз без них: последний срабатывавший пересчёт не видел
    # ни одной поддержки.
    if file_type in ("stl", "stl_supports"):
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
                # Cross-device copy (tmpfs -> bind mount) of up to 2 GB.
                await asyncio.to_thread(shutil.move, tmp_path, target)
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
    """Stream a stored file back (used by the dashboard STL viewer).

    Streamed in chunks rather than read whole: attachments are capped at 600 MB
    and the api container at 4 GB across 2 workers, so a couple of concurrent
    downloads of large STLs could exhaust it.
    """
    from fastapi.responses import StreamingResponse

    files = repo.list_print_files(record_id)
    match = next((f for f in files if f["file_id"] == file_id), None)
    if not match:
        raise HTTPException(404, "Файл не найден")

    uri = match["object_uri"]  # s3://bucket/object_name
    bucket, _, object_name = uri.removeprefix("s3://").partition("/")
    stream = ObjectStore().open_stream(bucket, object_name)
    if stream is None:
        raise HTTPException(503, "Файл недоступен в хранилище")

    content_type = mimetypes.guess_type(match["file_name"])[0] or "application/octet-stream"
    headers = {"Content-Disposition": _content_disposition(match["file_name"])}
    if match.get("size_bytes"):
        headers["Content-Length"] = str(match["size_bytes"])
    return StreamingResponse(stream, media_type=content_type, headers=headers)
