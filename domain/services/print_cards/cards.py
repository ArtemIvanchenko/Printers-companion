"""Print-card use cases; HTTP requests and response types stay in the API."""

from datetime import datetime, timezone
import logging
from sqlalchemy import select

from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from domain.services.estimation.inputs import require_local_print
from domain.services.print_cards.contracts import CardError, DeletionResult
from domain.services.print_cards.validation import (
    clean_material,
    parse_iso_datetime,
    parse_powder_cost,
    parse_layer_thickness,
    parse_hatch_distance,
)
from parsers.common.timestamps import date_hint_datetime
from storage.repositories.prints_repo import (
    PrintsRepository,
    PrintRecordConflict,
    PrintSessionLinkConflict,
)

logger = logging.getLogger(__name__)
_STATUSES = {"draft", "active", "completed"}


def create_card(
    repo: PrintsRepository, payload: dict, *, compute_node_id: str, actor: str | None
) -> dict:
    """Create a print record.

    Body: {name, material?, layer_thickness_mm?, hatch_distance_mm?, notes?,
    printed_at?, powder_cost_rub_per_kg?}. When printed_at is omitted, a date
    embedded in the name is used if found; the linked log session overwrites it
    later with the real start time.
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise CardError("invalid_inputs", "Поле 'name' обязательно")
    material = clean_material(payload.get("material") or "steel")
    printed_at = parse_iso_datetime(payload.get("printed_at"), "printed_at") or date_hint_datetime(
        name
    )

    record = repo.create_print_record(
        {
            "origin_compute_node_id": compute_node_id,
            "name": name,
            "material": material,
            "layer_thickness_mm": parse_layer_thickness(payload.get("layer_thickness_mm")),
            "hatch_distance_mm": parse_hatch_distance(payload.get("hatch_distance_mm")),
            "notes": (payload.get("notes") or "").strip() or None,
            "printed_at": printed_at,
            "powder_cost_rub_per_kg": parse_powder_cost(payload.get("powder_cost_rub_per_kg")),
            "updated_by": actor,
        }
    )
    repo.flush()
    logger.info("prints: created %s (%s)", record["record_id"], name)
    return record


def update_card(
    repo: PrintsRepository,
    record_id: str,
    payload: dict,
    *,
    compute_node_id: str,
    actor: str | None,
) -> dict:
    """Partial update: name, material, layer thickness, hatch distance, notes,
    status, session_id, printed_at, powder cost."""
    values: dict = {}
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise CardError("invalid_inputs", "Поле 'name' не может быть пустым")
        values["name"] = name
    if "material" in payload:
        values["material"] = clean_material(payload["material"])
    if "layer_thickness_mm" in payload:
        values["layer_thickness_mm"] = parse_layer_thickness(payload["layer_thickness_mm"])
    if "hatch_distance_mm" in payload:
        values["hatch_distance_mm"] = parse_hatch_distance(payload["hatch_distance_mm"])
    if "status" in payload:
        status = (payload["status"] or "").strip().lower()
        if status not in _STATUSES:
            raise CardError(
                "invalid_inputs", f"Недопустимый статус. Допустимы: {', '.join(sorted(_STATUSES))}"
            )
        values["status"] = status
    if "notes" in payload:
        values["notes"] = (payload["notes"] or "").strip() or None
    if "session_id" in payload:
        current_record = repo.get_print_record(record_id)
        if current_record is None:
            raise CardError("not_found", "Карточка печати не найдена")
        require_local_print(current_record, compute_node_id=compute_node_id)
        new_session_id = payload["session_id"] or None
        values["session_id"] = new_session_id
        metadata = dict(current_record.get("metadata_json") or {})
        metadata["session_link_confirmed"] = bool(new_session_id)
        if new_session_id:
            metadata["session_link_evidence"] = {
                "method": "operator_selected_session",
                "session_id": new_session_id,
                "eligible": True,
                "auto_link_allowed": True,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "confirmed_by": actor,
            }
        else:
            metadata.pop("session_link_evidence", None)
        values["metadata_json"] = metadata
        if new_session_id and "printed_at" not in payload:
            from domain.models.sessions import BuildSession

            session = repo.db.get(BuildSession, new_session_id)
            if session is not None:
                try:
                    require_compute_owner(
                        entity_type="session",
                        entity_id=session.session_id,
                        origin_compute_node_id=session.origin_compute_node_id,
                        requested_compute_node_id=compute_node_id,
                    )
                except ComputeAffinityError as exc:
                    raise CardError(
                        "conflict",
                        "Нельзя связать карточку с сессией другого ПК. " + str(exc),
                    ) from exc
            if session and session.start_ts:
                ts = session.start_ts
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                values["printed_at"] = ts
    if "printed_at" in payload:
        values["printed_at"] = parse_iso_datetime(payload["printed_at"], "printed_at")
    if "powder_cost_rub_per_kg" in payload:
        values["powder_cost_rub_per_kg"] = parse_powder_cost(payload["powder_cost_rub_per_kg"])
    if not values:
        raise CardError("invalid_inputs", "Нет полей для обновления")
    if actor:
        values["updated_by"] = actor

    if "expected_revision" not in payload:
        raise CardError(
            "precondition_required",
            "Для изменения общей карточки обязателен expected_revision",
        )
    expected_revision = payload["expected_revision"]
    if isinstance(expected_revision, bool):
        raise CardError(
            "invalid_inputs", "expected_revision должен быть целым положительным числом"
        )
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError):
        raise CardError(
            "invalid_inputs", "expected_revision должен быть целым положительным числом"
        ) from None
    if expected_revision < 1:
        raise CardError(
            "invalid_inputs", "expected_revision должен быть целым положительным числом"
        )
    try:
        record = repo.update_print_record(
            record_id,
            values,
            expected_revision=expected_revision,
        )
    except PrintRecordConflict as conflict:
        raise CardError(
            "conflict",
            detail={
                "message": "Карточка уже изменена на другом рабочем месте",
                "current": conflict.current,
            },
        ) from None
    except PrintSessionLinkConflict as conflict:
        raise CardError(
            "conflict",
            f"Сессия не может быть привязана к карточке: {conflict}",
        ) from None
    if not record:
        raise CardError("not_found", "Карточка печати не найдена")
    repo.flush()
    # Changed or removed links and corrected modes invalidate calibration too.
    # The short save only queues work; it never reads logs or fits models.
    if {"session_id", "material", "layer_thickness_mm", "hatch_distance_mm"} & values.keys():
        from analytics.prediction.calibration import enqueue_calibration

        enqueue_calibration(
            repo.db,
            owner_node_id=record["origin_compute_node_id"],
            trigger="print_changed",
            event_id=f"print:{record_id}:{record['revision']}",
        )
    if values.get("session_id"):
        from analytics.prediction.retraining import enqueue_retraining_for_session

        try:
            enqueue_retraining_for_session(repo.db, str(values["session_id"]))
        except Exception:
            logger.exception("auto-retraining enqueue after manual link failed")
    return record


def get_card(repo: PrintsRepository, record_id: str) -> dict:
    """Read-only use case; releases its transaction before geometry mapping.

    Pass a clean repository, not a transaction containing unpublished writes.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise CardError("not_found", "Карточка печати не найдена")
    record["files"] = repo.list_print_files(record_id)
    from domain.models.jobs import BackgroundJob

    estimate_job = repo.db.scalar(
        select(BackgroundJob)
        .where(
            BackgroundJob.entity_id == record_id,
            BackgroundJob.job_type == "print_estimate",
            BackgroundJob.status.in_(["pending", "running", "postponed"]),
        )
        .order_by((BackgroundJob.status == "running").desc(), BackgroundJob.created_at.desc())
        .limit(1)
    )
    record["estimate_job"] = (
        {"job_id": estimate_job.job_id, "status": estimate_job.status} if estimate_job else None
    )
    from storage.repositories.runtime import RuntimeRepository

    record["quality_outcomes"] = RuntimeRepository(repo.db).list_quality_outcomes(
        print_record_id=record_id,
    )
    snapshot = (record.get("metadata_json") or {}).get("prediction") or {}
    if record.get("session_id") and snapshot.get("scan_geometry"):
        input_revision = snapshot.get("input_revision")
        snapshot_is_current = not isinstance(input_revision, int) or record.get("revision") in {
            input_revision,
            input_revision + 1,
        }
        if not snapshot_is_current:
            record["geometry_analysis"] = {
                "status": "stale_prediction",
                "reason_ru": (
                    "Карточка или STL изменились после расчёта; привязка аномалий скрыта "
                    "до повторного расчёта."
                ),
                "items": [],
            }
            repo.db.rollback()
            return record
        from analytics.geometry_context import map_anomalies_to_geometry
        from domain.models.sessions import BuildSession

        session = repo.db.get(BuildSession, record["session_id"])
        group = (
            (((session.context or {}).get("runtime_payload") or {}).get("group") or {})
            if session is not None
            else {}
        )
        repo.db.rollback()
        try:
            record["geometry_analysis"] = map_anomalies_to_geometry(
                group.get("health"),
                snapshot.get("scan_geometry"),
                geometry_regions=snapshot.get("geometry_regions"),
                telemetry=group.get("telemetry"),
                geometry_quality=snapshot.get("geometry_quality"),
            )
        except Exception:
            # This is derived display context over two stored snapshots. A bad
            # legacy snapshot must not make the print card itself unreadable.
            logger.exception("prints: geometry anomaly mapping failed for %s", record_id)
            record["geometry_analysis"] = {
                "status": "unavailable",
                "reason_ru": "Не удалось сопоставить старый снимок геометрии с логами",
                "items": [],
            }
    repo.db.rollback()
    return record


def delete_card(repo: PrintsRepository, record_id: str) -> DeletionResult:
    """Own the catalogue commit; return cleanup work only after it succeeds."""
    record = repo.get_print_record_for_update(record_id)
    if not record:
        raise CardError("not_found", "Карточка печати не найдена")
    uris = repo.delete_print_record(record_id)
    repo.flush()
    if record.get("session_id"):
        from analytics.prediction.calibration import enqueue_calibration

        enqueue_calibration(
            repo.db,
            owner_node_id=record["origin_compute_node_id"],
            trigger="print_deleted",
            event_id=f"deleted:{record_id}",
        )
    # This use-case owns the commit: HTTP background tasks may run before
    # request-scoped dependencies close. Never expose cleanup before commit.
    try:
        repo.db.commit()
    except Exception:
        repo.db.rollback()
        raise
    logger.info("prints: deleted %s (%d files)", record_id, len(uris))
    return DeletionResult({"deleted": record_id, "files_removed": len(uris)}, uris)
