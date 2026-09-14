"""Owner-local calibration: capture evidence, compute, fence, publish.

Only the worker invokes calculate_calibration. HTTP and import writers enqueue
durable jobs; the accuracy page may calculate descriptive statistics, never fits.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.prediction.accuracy import apply_accuracy_report, prediction_accuracy, try_acquire_calibration_lock
from analytics.prediction.calibration_inputs import CalibrationInputs, load_calibration_inputs
from analytics.prediction.recoat_calibration import apply_recoat_report, recoat_accuracy
from analytics.prediction.scan_calibration import apply_scan_report, scan_calibration_report
from core.versioning.provenance import build_provenance, stable_hash
from core.versioning.constants import ANALYSIS_VERSION
from domain.models.jobs import BackgroundJob
from domain.models.prints import MachineParams
from storage.repositories.jobs_repo import JobsRepository

JOB_TYPE = "time_calibration"
CONTRACT_VERSION = 2
REPORT_LIMIT = 100


class CalibrationInputsChanged(RuntimeError):
    """Retry from a fresh snapshot; the previous result must not be applied."""


def enqueue_calibration(
    db: Session, *, owner_node_id: str, trigger: str, event_id: str | None = None,
) -> dict[str, Any]:
    """Coalesce pending work, but never drop a trigger arriving during a fit."""
    pending_id = db.scalar(select(BackgroundJob.job_id).where(
        BackgroundJob.job_type == JOB_TYPE,
        BackgroundJob.owner_node_id == owner_node_id,
        BackgroundJob.status == "pending",
    ).order_by(BackgroundJob.created_at, BackgroundJob.job_id)
        .with_for_update(skip_locked=True).limit(1))
    if pending_id is not None:
        return JobsRepository(db).get(pending_id)
    return JobsRepository(db).enqueue(
        job_type=JOB_TYPE, owner_node_id=owner_node_id,
        entity_type="calibration", entity_id="machine-time",
        idempotency_key=f"{JOB_TYPE}:{owner_node_id}:{stable_hash(event_id or uuid4().hex)[:32]}",
        payload={"owner_node_id": owner_node_id, "trigger": trigger,
                 "contract_version": CONTRACT_VERSION},
    )


def calculate_calibration(inputs: CalibrationInputs, *, owner_node_id: str) -> dict:
    """CPU-only calculation on detached shared evidence; no SQL/file handles."""
    result = {
        "input_fingerprint": inputs.input_fingerprint,
        "config_fingerprint": inputs.config_fingerprint,
        "contract_version": CONTRACT_VERSION,
        "provenance": build_provenance(
            "time-calibration", inputs={"fingerprint": inputs.input_fingerprint},
            config={"params": inputs.params, "contract_version": CONTRACT_VERSION},
            model_versions={"scan": "nnls-geometry", "cycle": "max-base-floor"},
            generated_by=owner_node_id,
        ),
    }
    result["provenance"]["input_fingerprint"] = inputs.input_fingerprint
    if inputs.params is None or inputs.params.get("correction_locked"):
        return {**result, "status": "locked" if inputs.params else "no_machine_params"}
    report = prediction_accuracy(inputs=inputs)
    recoat = recoat_accuracy(inputs=inputs)
    scan = scan_calibration_report(inputs=inputs)
    for models in (scan["candidates"], scan["cycle_candidates"]):
        for model in models.values():
            if model.get("status") == "ok":
                model["provenance"] = result["provenance"]
    return {**result, "status": "calculated", "accuracy": report, "recoat": recoat, "scan": scan}


def _compact_scan_report(report: dict) -> dict:
    """Bound job JSON, never persist the full training/geometry arrays."""
    result = deepcopy(report)
    result["n_records"] = len(result.get("records", []))
    result["records"] = result.get("records", [])[:REPORT_LIMIT]
    for name in ("candidates", "cycle_candidates"):
        models = result.get(name, {})
        result[f"n_{name}"] = len(models)
        result[name] = dict(list(models.items())[:REPORT_LIMIT])
        for model in result[name].values():
            for key in ("source_records", "source_geometry_fingerprints"):
                if key in model:
                    model[f"n_{key}"] = len(model[key])
                    model[key] = model[key][:REPORT_LIMIT]
    result["report_limit"] = REPORT_LIMIT
    return result


def publish_calibration(
    db: Session, calculated: dict, *, job_id: str, lease_owner: str, lease_generation: int,
) -> dict | None:
    """Validate inputs, then fence the job immediately before publication.

    Compare evidence and all machine settings under short parent/params locks.
    A late worker cannot overwrite a manual edit or another PC's newer model.
    """
    if not try_acquire_calibration_lock(db):
        raise CalibrationInputsChanged("Другая калибровка публикуется; повторим расчёт позже.")
    from storage.repositories.prints_repo import lock_estimation_configuration

    lock_estimation_configuration(db)
    current = load_calibration_inputs(db, for_update=True)
    if (current.input_fingerprint != calculated["input_fingerprint"]
            or current.config_fingerprint != calculated["config_fingerprint"]):
        raise CalibrationInputsChanged("Данные или настройки изменились; нужен свежий расчёт.")
    # Check after waiting for source locks, not before: the lease can expire
    # while another short publication owns the parameter row.
    if JobsRepository(db).complete(job_id, {}, lease_owner=lease_owner,
                                   lease_generation=lease_generation) is None:
        return None
    common = {key: calculated[key] for key in (
        "input_fingerprint", "config_fingerprint", "contract_version", "provenance",
    )}
    if calculated["status"] != "calculated":
        return {**common, "status": calculated["status"], "applied": {},
                "locked": calculated["status"] == "locked"}
    row = db.get(MachineParams, 1)
    result = apply_accuracy_report(row, calculated["accuracy"])
    result["recoat"] = apply_recoat_report(row, calculated["recoat"])
    result["scan"] = apply_scan_report(row, calculated["scan"])
    # Durable jobs carry bounded diagnostics; the coefficient maps live in
    # MachineParams. Avoid duplicating unbounded model/source lists in history.
    result["scan"] = {key: (sorted(value) if isinstance(value, dict) else value[:REPORT_LIMIT]
                           if isinstance(value, list) else value)
                      for key, value in result["scan"].items()}
    return {**common, **result, "status": "published",
            "scan_report": _compact_scan_report(calculated["scan"])}


def latest_calibration_report(db: Session) -> dict | None:
    """Last published compact report, shared by every workstation."""
    return db.scalar(select(BackgroundJob.result_json).where(
        BackgroundJob.job_type == JOB_TYPE,
        BackgroundJob.status == "done",
        BackgroundJob.result_json["status"].as_string() == "published",
    ).order_by(BackgroundJob.finished_at.desc(), BackgroundJob.job_id.desc()).limit(1))


def accuracy_view(inputs: CalibrationInputs, published: dict | None) -> dict:
    """Fresh descriptive accuracy + explicitly dated cached model diagnostics."""
    report = prediction_accuracy(inputs=inputs)
    report["recoat"] = recoat_accuracy(inputs=inputs)
    cached = (published or {}).get("scan_report") or {}
    current = bool(published and published.get("input_fingerprint") == inputs.input_fingerprint
                   and published.get("contract_version") == CONTRACT_VERSION
                   and (published.get("provenance") or {}).get("analysis_version") == ANALYSIS_VERSION)
    report["scan"] = {
        **cached,
        # Never display stale fit metrics as a candidate for the current data.
        "candidates": cached.get("candidates", {}) if current else {},
        "cycle_candidates": cached.get("cycle_candidates", {}) if current else {},
        "status": "current" if current else "stale" if published else "not_calculated",
        "status_ru": ("Последний фоновый расчёт соответствует данным." if current else
                      "Данные изменились — нужен новый фоновый расчёт." if published else
                      "Фоновая калибровка ещё не выполнялась."),
        "provenance": (published or {}).get("provenance"),
    }
    report["timing_source"] = "shared_layer_snapshots"
    report["missing_timing_sessions"] = sorted({session.session_id for _, session in inputs.linked
                                               if session.session_id not in inputs.events})
    return report
