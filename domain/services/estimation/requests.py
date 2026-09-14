"""Cheap admission and owner-local idempotent estimate queue submission."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from core.config.settings import get_settings
from domain.services.estimation.contracts import EstimateError
from domain.services.estimation.inputs import (
    _assert_geometry_usable,
    require_local_print,
    missing_for_estimation,
    params_for_record,
)
from storage.repositories.prints_repo import PrintsRepository


def assert_estimatable(
    repo: PrintsRepository, record: dict, *, compute_node_id: str | None = None
) -> None:
    """Raise if the estimate cannot run, before committing to a long job.

    Only the cheap preconditions — an attached STL and the machine parameters.
    Running these up front means the operator hears "no STL attached" straight
    away instead of watching a background job produce nothing.
    """
    require_local_print(record, compute_node_id=compute_node_id)
    _assert_geometry_usable(record)

    files = repo.list_print_files(record["record_id"])
    if not [f for f in files if f["file_type"] in ("stl", "stl_supports")]:
        raise EstimateError("invalid_inputs", "К карточке не прикреплён STL")

    # Same resolution the real estimate uses, or this precondition reports a
    # parameter as missing that the record itself supplies.
    missing = missing_for_estimation(params_for_record(repo, record))
    if missing:
        raise EstimateError(
            "invalid_inputs",
            "Для расчёта не хватает параметров машины: "
            + ", ".join(missing)
            + ". Заполните их в Настройки → Параметры машины.",
        )


def enqueue_estimate(
    repo: PrintsRepository,
    record: dict,
    *,
    force: bool = False,
    compute_node_id: str | None = None,
) -> dict[str, Any]:
    """Create a local estimate job for this exact geometry/revision.

    Automatic triggers deduplicate the same inputs. A manual "recalculate"
    always gets a fresh request id, even when the card revision is unchanged.
    """
    from storage.repositories.jobs_repo import JobsRepository

    node_id = compute_node_id or get_settings().compute_node_id
    require_local_print(record, compute_node_id=node_id)
    geometry = sorted(
        (f["file_type"], f["checksum"])
        for f in repo.list_print_files(record["record_id"])
        if f["file_type"] in ("stl", "stl_supports")
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {"revision": record["revision"], "geometry": geometry},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    request_key = os.urandom(12).hex() if force else fingerprint
    return JobsRepository(repo.db).enqueue(
        job_type="print_estimate",
        owner_node_id=node_id,
        entity_type="print_record",
        entity_id=record["record_id"],
        idempotency_key=(f"print_estimate:{node_id}:{record['record_id']}:{request_key}"),
        payload={
            "record_id": record["record_id"],
            "record_revision": record["revision"],
            "owner_node_id": node_id,
            "input_fingerprint": fingerprint,
            "manual_rerun": force,
        },
        max_attempts=3,
    )
