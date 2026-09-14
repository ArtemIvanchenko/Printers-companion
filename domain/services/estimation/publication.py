"""Store a prediction and atomically publish a fenced durable estimate."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from domain.services.estimation.contracts import EstimateError
from domain.services.estimation.inputs import (
    require_local_print,
    prepare_prediction_inputs,
    prediction_input_hash,
)
from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import (
    PrintsRepository,
    PrintRecordConflict,
    lock_estimation_configuration,
)

logger = logging.getLogger(__name__)


def _require_lease(db, *, job_id, record_id, owner_node_id, lease_owner, lease_generation):
    row = db.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    until = row.lease_until if row else None
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if (
        row is None
        or row.status != "running"
        or row.job_type != "print_estimate"
        or row.owner_node_id != owner_node_id
        or row.lease_owner != lease_owner
        or row.lease_generation != lease_generation
        or row.entity_type != "print_record"
        or row.entity_id != record_id
        or (row.payload_json or {}).get("record_id") != record_id
        or (row.payload_json or {}).get("owner_node_id") != owner_node_id
        or until is None
        or until <= datetime.now(timezone.utc)
    ):
        raise EstimateError(
            "lease_lost",
            "Право на расчёт истекло или передано другому обработчику; результат не сохранён.",
        )
    return row


def publish_estimate(
    db,
    prepared,
    snapshot,
    *,
    job_id: str,
    owner_node_id: str,
    lease_owner: str,
    lease_generation: int,
) -> None:
    """Recheck inputs and fresh lease; write the card and completion atomically.

    The caller owns the transaction. Raising (including at the last lease
    check) rolls back the card as well as job state. No file/ML/history IO here.
    """
    record_id = prepared["record"]["record_id"]
    fence = dict(
        job_id=job_id,
        record_id=record_id,
        owner_node_id=owner_node_id,
        lease_owner=lease_owner,
        lease_generation=lease_generation,
    )
    claimed = _require_lease(db, **fence)
    if (claimed.payload_json or {}).get("record_revision") != prepared["record"]["revision"]:
        raise EstimateError("stale_inputs", "Задание относится к другой версии карточки.")
    # Configuration -> linked session -> card is the common publication order.
    # The configuration guard also fences default-preset insertion/deletion.
    lock_estimation_configuration(db)
    sid = prepared["record"].get("session_id")
    if sid:
        db.execute(
            select(BuildSession)
            .where(BuildSession.session_id == sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    db.execute(
        select(PrintRecord)
        .where(PrintRecord.record_id == record_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    repo = PrintsRepository(db)
    current = prepare_prediction_inputs(repo, record_id, compute_node_id=owner_node_id)
    expected_hash = prediction_input_hash(prepared)
    if (
        prediction_input_hash(current) != expected_hash
        or snapshot.get("input_hash") != expected_hash
        or snapshot.get("input_revision") != prepared["record"]["revision"]
    ):
        raise EstimateError(
            "stale_inputs",
            "Карточка, файлы или параметры машины изменились во время расчёта. Запустите новый расчёт.",
        )
    _require_lease(db, **fence)  # locks may have waited past the lease deadline
    store_prediction_snapshot(
        repo,
        record_id,
        snapshot,
        expected_revision=prepared["record"]["revision"],
        compute_node_id=owner_node_id,
    )
    if (
        JobsRepository(db).complete(
            job_id,
            {"record_id": record_id, "prediction": snapshot},
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )
        is None
    ):
        raise EstimateError(
            "lease_lost", "Право на расчёт истекло при записи; результат не сохранён."
        )


def store_prediction_snapshot(
    repo: PrintsRepository,
    record_id: str,
    snapshot: dict,
    *,
    expected_revision: int | None = None,
    compute_node_id: str | None = None,
) -> None:
    record = repo.get_print_record(record_id)
    if not record:
        raise EstimateError("not_found", "Карточка печати не найдена")
    require_local_print(record, compute_node_id=compute_node_id)

    meta = dict(record.get("metadata_json") or {})
    meta["prediction"] = snapshot
    try:
        repo.update_print_record(
            record_id,
            {"metadata_json": meta},
            expected_revision=expected_revision,
        )
    except PrintRecordConflict as conflict:
        raise EstimateError(
            "stale_inputs",
            detail={
                "message": "Карточка изменилась во время расчёта; устаревший результат отброшен",
                "current": conflict.current,
            },
        ) from None
    repo.flush()
    if record.get("session_id"):
        from analytics.prediction.calibration import enqueue_calibration

        enqueue_calibration(
            repo.db,
            owner_node_id=record["origin_compute_node_id"],
            trigger="prediction_saved",
            event_id=f"prediction:{record_id}:{snapshot.get('estimated_at')}:{expected_revision}",
        )
    logger.info(
        "prints: prediction stored for %s (%d parts + %d supports, %.1fh, ×%.3f)",
        record_id,
        int(snapshot.get("n_parts") or 0),
        int(snapshot.get("n_supports") or 0),
        snapshot["print_hours"],
        snapshot["correction_factor"],
    )
