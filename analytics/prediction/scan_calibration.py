"""Fit scan-time models from real per-layer burn_ms in printer logs (Level B).

Ground truth: ``*_time.log`` records, per physical layer, the machine's own
measured ``burn_ms`` (scan duration). Predictor: the co-hatched per-layer
geometry series the estimate stored in the prediction snapshot
(``metadata_json["prediction"]["scan_geometry"]``) at estimate time — pairing
them needs no re-slicing, just interpolation of the stored series at each
layer's height.

Model: per-layer ``burn_seconds ≈ Σ beta_k · g_k / laser_count + intercept``
over ``layer_engine.GEOMETRY_FEATURES``, fitted with non-negative least squares
(physics forbids negative time per unit length). Quality is reported from the
current database rather than frozen example numbers in source code.

Two hard-won honesty rules, both from real-data validation:

* The betas are NOT physical speeds. Geometry components are strongly
  collinear (they all grow with cross-section size), so NNLS concentrates
  weight arbitrarily among them. Only the fitted linear MAP is identifiable —
  never report ``1/beta`` as a speed.
* Scan models are keyed by a captured physical-machine/configuration scope.
  Legacy models without the scope remain historical data, not reusable models.
  Configured inputs do not prove that the machine executed that actual recipe.

Robustness to the known multi-day log-splitting bug: a session holding only
part of a print's layers still yields valid (geometry, burn) pairs for the
layers it has. Acceptance requires at least two independent geometry
fingerprints and leave-one-geometry-out validation, so an exact reprint cannot
appear in both train and validation. Normal controller cycle calibration is a
separate robust model over burn+pour→make and does not depend on accepted STL
scan geometry.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session
from analytics.prediction.calibration_inputs import CalibrationInputs

from analytics.prediction.accuracy import (
    PRINT_CLASSIFICATIONS,
    calibration_is_excluded,
    iter_linked_prints,
    session_classification,
)
from analytics.prediction.layer_engine import (
    LayerGeometrySeries,
    machine_mode_key,
)
from analytics.prediction.input_quality import INPUT_REASON_RU, calibration_input_exclusion
from analytics.prediction.scan_scope import scan_scope_key, snapshot_scan_scope
from analytics.prediction.timing_validation import (
    MAX_BURN_MS, MIN_BURN_MS, calibration_burn_ms, calibration_cycles_ms, calibration_timing_payloads,
)
# Stable compatibility exports for existing tests/research callers. The local
# numerical module has no SQL, object-store, parser or HTTP responsibilities.
from analytics.prediction.scan_fitting import (
    MIN_LAYERS_FOR_FIT as MIN_LAYERS_FOR_FIT,
    MIN_PRINTS_FOR_FIT as MIN_PRINTS_FOR_FIT,
    MIN_FIT_R2 as MIN_FIT_R2,
    MAX_TOTAL_ERR_PCT as MAX_TOTAL_ERR_PCT,
    MIN_FIT_R2_FLOOR as MIN_FIT_R2_FLOOR,
    TIGHT_TOTAL_ERR_PCT as TIGHT_TOTAL_ERR_PCT,
    MAX_CV_MEDIAN_TOTAL_ERR_PCT as MAX_CV_MEDIAN_TOTAL_ERR_PCT,
    MAX_CV_WORST_TOTAL_ERR_PCT as MAX_CV_WORST_TOTAL_ERR_PCT,
    MIN_CYCLE_BRANCH_LAYERS as MIN_CYCLE_BRANCH_LAYERS,
    MIN_CYCLE_BRANCH_PRINTS as MIN_CYCLE_BRANCH_PRINTS,
    _MAX_BASE_OVERHEAD_MS as _MAX_BASE_OVERHEAD_MS,
    _MAX_MINIMUM_CYCLE_MS as _MAX_MINIMUM_CYCLE_MS,
    _MAX_CYCLE_TRAINING_RESIDUAL_MS as _MAX_CYCLE_TRAINING_RESIDUAL_MS,
    _CYCLE_ROBUST_F_SCALE_MS as _CYCLE_ROBUST_F_SCALE_MS,
    _MIN_FLOOR_ROBUST_LOSS_IMPROVEMENT_PCT as _MIN_FLOOR_ROBUST_LOSS_IMPROVEMENT_PCT,
    _group_weights as _group_weights,
    _fit_beta as _fit_beta,
    _fit as _fit,
    _gate as _gate,
    _fit_layer_cycle_model as _fit_layer_cycle_model,
)

from domain.models.prints import MachineParams

logger = logging.getLogger(__name__)

# Plausibility bounds on a single layer's burn reading (ms) — mirrors the
# pour_ms guards in recoat_calibration.
_MIN_BURN_MS, _MAX_BURN_MS = MIN_BURN_MS, MAX_BURN_MS

def _burn_seconds_by_layer(events: list[Any]) -> dict[int, float]:
    """{layer: burn_seconds}; conflicting repeated attempts are excluded."""
    timings = calibration_timing_payloads(events)
    return {layer: burn / 1000.0 for layer, burn in calibration_burn_ms(timings).items()}


def _layer_overhead_ms_by_layer(events: list[Any]) -> dict[int, float]:
    """Validated ``make_layer - burn - pour`` residual per physical layer."""
    from analytics.prediction.layer_timings import MAX_LAYER_OVERHEAD_MS

    out: dict[int, float] = {}
    for layer, (burn_ms, pour_ms, make_layer_ms) in _layer_cycles_ms_by_layer(events).items():
        overhead_ms = make_layer_ms - burn_ms - pour_ms
        if 0.0 <= overhead_ms <= MAX_LAYER_OVERHEAD_MS:
            out[layer] = overhead_ms
    return out


def _layer_cycles_ms_by_layer(
    events: list[Any],
) -> dict[int, tuple[float, float, float]]:
    """Raw valid cycles; conflicting repeated attempts are excluded.

    Unlike ``_layer_overhead_ms_by_layer`` this deliberately retains a long
    pause-like ``make`` value.  It is evidence and may be useful to explain the
    finished build; only the normal-cycle fitter filters it out.
    """
    return calibration_cycles_ms(calibration_timing_payloads(events))


def session_burn_by_layer(session_id: str, db: Session) -> dict[int, float] | None:
    """Per-layer burn: shared measured evidence, then owner-local legacy raw."""
    from analytics.prediction.layer_timings import legacy_session_timing_events

    return _burn_seconds_by_layer(legacy_session_timing_events(session_id, db)) or None


def session_layer_overhead_ms_by_layer(session_id: str, db: Session) -> dict[int, float] | None:
    """Validated inter-phase residual, with the common measured-source policy."""
    from analytics.prediction.layer_timings import legacy_session_timing_events

    return _layer_overhead_ms_by_layer(legacy_session_timing_events(session_id, db)) or None


def session_layer_cycles_ms_by_layer(
    session_id: str, db: Session,
) -> dict[int, tuple[float, float, float]] | None:
    """Raw measured complete cycles; phase/normal-cycle admission is separate."""
    from analytics.prediction.layer_timings import legacy_session_timing_events

    return _layer_cycles_ms_by_layer(legacy_session_timing_events(session_id, db)) or None


def _pairs_from_record(snapshot: dict, burn: dict[int, float]) -> tuple[list[list[float]], list[float]] | None:
    """(X rows, y) for one record: stored geometry interpolated at each layer."""
    geo = snapshot.get("scan_geometry")
    if not isinstance(geo, dict):
        return None
    thickness = geo.get("layer_thickness_mm")
    laser_count = int(geo.get("laser_count") or 1)
    if not thickness or thickness <= 0:
        return None
    try:
        series = LayerGeometrySeries.from_snapshot(geo)
    except (KeyError, TypeError, ValueError):
        return None

    points = []
    for layer, burn_s in sorted(burn.items()):
        z = series.z_min + (layer - 0.5) * thickness
        if not (series.zs[0] <= z <= series.zs[-1]):
            continue
        points.append((z, burn_s))
    if not points:
        return None
    geometry = series.at_heights([z for z, _ in points]).tolist()
    return (
        [[value / max(laser_count, 1) for value in components] + [1.0]
         for components in geometry],
        [burn_s for _, burn_s in points],
    )


def _geometry_fingerprint(snapshot: dict) -> str | None:
    """Stable geometry identity for leakage-safe validation.

    New snapshots carry the SHA built from sorted ``file_type + file SHA``.
    Old snapshots predate that field, so their canonical stored geometry is a
    conservative fallback: exact reprints remain in the same CV fold instead
    of leaking a twin into both train and validation.
    """
    value = snapshot.get("geometry_fingerprint")
    if isinstance(value, str) and value.strip():
        return value.strip()
    geometry = snapshot.get("scan_geometry")
    if not isinstance(geometry, dict):
        return None
    canonical = json.dumps(
        geometry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode("utf-8")
    return "legacy-scan-geometry:" + hashlib.sha256(canonical).hexdigest()


def scan_calibration_report(db: Session | None = None, *, inputs: CalibrationInputs | None = None) -> dict:
    """Collect (geometry, burn) pairs per mode and fit candidate models."""
    rows: list[dict] = []
    # One (X_rows, y) group per contributing print, not a flat pool — _fit
    # weights each print's group equally regardless of its own layer count.
    by_key: dict[
        str, list[tuple[list[list[float]], list[float], str, str]]
    ] = defaultdict(list)
    cycle_by_key: dict[
        str, list[tuple[list[float], list[float], str, str]]
    ] = defaultdict(list)
    scopes: dict[str, dict] = {}

    linked = inputs.linked if inputs is not None else list(iter_linked_prints(db))
    link_counts = Counter(record.session_id for record, _ in linked)
    for record, session in linked:
        snapshot = (record.metadata_json or {}).get("prediction") or {}
        if session_classification(session) not in PRINT_CLASSIFICATIONS:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "not_a_print"})
            continue
        if link_counts[record.session_id] > 1:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "duplicate_session_link"})
            continue

        geo = snapshot.get("scan_geometry")
        material = (snapshot.get("material") or record.material or "—")
        thickness_value = snapshot.get("layer_thickness_mm")
        if thickness_value is None and isinstance(geo, dict):
            thickness_value = geo.get("layer_thickness_mm")
        if thickness_value is None:
            thickness_value = record.layer_thickness_mm
        try:
            thickness = float(thickness_value)
        except (TypeError, ValueError):
            thickness = 0.0
        laser_value = snapshot.get("laser_count")
        if laser_value is None and isinstance(geo, dict):
            laser_value = geo.get("laser_count")
        try:
            laser_count = max(int(laser_value or 1), 1)
        except (TypeError, ValueError):
            laser_count = 1
        # The linked measurement identifies the physical machine. A conflicting
        # old prediction cannot relabel its timings as another machine's data.
        printer_id = session.printer_id
        if (not isinstance(printer_id, str) or not printer_id.strip()
                or snapshot.get("printer_id") not in (None, printer_id)):
            printer_id = None
        key = (
            machine_mode_key(
                str(printer_id) if printer_id else None,
                material,
                thickness,
                laser_count,
            )
            if thickness > 0 and printer_id else None
        )

        # Full-cycle calibration needs only machine timings and a mode. It is
        # intentionally independent of STL completeness and NNLS scan gates.
        cycles = (
            None if calibration_is_excluded(record, "cycle")
            else (inputs.cycles.get(record.session_id) if inputs is not None
                  else session_layer_cycles_ms_by_layer(record.session_id, db))
        )
        cycle_used = False
        if cycles and key is not None:
            cycle_fingerprint = _geometry_fingerprint(snapshot) or "unknown-geometry"
            cycle_by_key[key].append((
                [burn_ms + pour_ms for burn_ms, pour_ms, _ in cycles.values()],
                [make_ms for _, _, make_ms in cycles.values()],
                record.record_id,
                cycle_fingerprint,
            ))
            cycle_used = True

        if calibration_is_excluded(record, "scan"):
            rows.append({
                "record_id": record.record_id,
                "session_id": record.session_id,
                "used": False,
                "cycle_used": cycle_used,
                "reason": "manually_excluded",
            })
            continue

        if not isinstance(geo, dict):
            rows.append({
                "record_id": record.record_id,
                "session_id": record.session_id,
                "used": False,
                "cycle_used": cycle_used,
                "reason": "no_scan_geometry",
            })
            continue
        exclusion = calibration_input_exclusion(
            record.metadata_json or {}, snapshot, record.revision,
            session_id=record.session_id,
        )
        if exclusion:
            rows.append({
                "record_id": record.record_id, "session_id": record.session_id,
                "used": False, "cycle_used": cycle_used, "reason": exclusion,
                "reason_ru": INPUT_REASON_RU[exclusion],
            })
            continue
        burn = (inputs.burns.get(record.session_id) if inputs is not None
                else session_burn_by_layer(record.session_id, db))
        if not burn:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "no_time_log"})
            continue
        pairs = _pairs_from_record(snapshot, burn)
        if pairs is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "geometry_snapshot_unusable"})
            continue

        scope = snapshot_scan_scope(snapshot, session.printer_id)
        if scope is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "cycle_used": cycle_used,
                         "reason": "scan_scope_unavailable",
                         "reason_ru": "Нет сохранённого состава настроек прожига и идентичности машины; нужен новый снимок."})
            continue
        key = scan_scope_key(scope)
        scopes[key] = scope
        fingerprint = _geometry_fingerprint(snapshot)
        if fingerprint is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "geometry_fingerprint_unavailable"})
            continue
        by_key[key].append((pairs[0], pairs[1], record.record_id, fingerprint))
        rows.append({"record_id": record.record_id, "session_id": record.session_id,
                     "used": True, "mode": key, "n_layers": len(pairs[1]),
                     "cycle_used": cycle_used, "geometry_fingerprint": fingerprint})

    candidates: dict[str, dict] = {}
    for key, groups in by_key.items():
        model = _fit(
            [(X_rows, y) for X_rows, y, _, _ in groups],
            [fingerprint for _, _, _, fingerprint in groups],
        )
        if model is None:
            candidates[key] = {"status": "fit_failed"}
            continue
        model["source_records"] = [rid for _, _, rid, _ in groups]
        model["scan_calibration_scope"] = scopes[key]
        model["source_geometry_fingerprints"] = sorted({
            fingerprint for _, _, _, fingerprint in groups
        })
        reason = _gate(model)
        model["status"] = "ok" if reason is None else f"rejected: {reason}"
        candidates[key] = model

    cycle_candidates: dict[str, dict] = {}
    for key, groups in cycle_by_key.items():
        cycle_model = _fit_layer_cycle_model(
            [(components, makes) for components, makes, _, _ in groups],
            [fingerprint for _, _, _, fingerprint in groups],
        )
        if cycle_model is None:
            cycle_candidates[key] = {"status": "fit_failed"}
            continue
        cycle_model["source_records"] = [record_id for _, _, record_id, _ in groups]
        if cycle_model["n_prints"] < MIN_PRINTS_FOR_FIT:
            status = (
                f"rejected: too_few_prints ({cycle_model['n_prints']} < "
                f"{MIN_PRINTS_FOR_FIT})"
            )
        elif cycle_model["n_geometries"] < 2:
            status = (
                "rejected: too_few_unique_geometries "
                f"({cycle_model['n_geometries']} < 2)"
            )
        elif cycle_model["n_layers"] < MIN_LAYERS_FOR_FIT:
            status = (
                f"rejected: too_few_layers ({cycle_model['n_layers']} < "
                f"{MIN_LAYERS_FOR_FIT})"
            )
        elif cycle_model["base_overhead_ms"] is None:
            status = "rejected: base_overhead_unidentified"
        else:
            status = "ok"
        cycle_model["status"] = status
        cycle_candidates[key] = cycle_model

    return {
        "records": rows,
        "candidates": candidates,
        "cycle_candidates": cycle_candidates,
        "min_layers_for_fit": MIN_LAYERS_FOR_FIT,
        "min_prints_for_fit": MIN_PRINTS_FOR_FIT,
        "min_r2": MIN_FIT_R2,
        "max_total_err_pct": MAX_TOTAL_ERR_PCT,
    }


def recalibrate_scan_and_apply(db: Session) -> dict:
    """Fit per-mode scan models from history and persist the ones passing gates.

    No-op when ``correction_locked`` (one operator lock for all auto-calibration).
    Caller commits.
    """
    return apply_scan_report(db.get(MachineParams, 1), scan_calibration_report(db))


def apply_scan_report(row: MachineParams | None, report: dict) -> dict:
    """Publish accepted coefficients only; fitting is a local worker concern."""
    if row is None:
        return {"applied": {}, "skipped": [], "locked": False, "reason": "no machine params"}
    if row.correction_locked:
        return {"applied": {}, "skipped": [], "locked": True}

    current = dict(row.scan_model_by_mat or {})
    current_cycles = dict(row.layer_cycle_model_by_mode or {})
    applied: dict[str, dict] = {}
    skipped: list[dict] = []
    removed: list[str] = []
    for key, model in report["candidates"].items():
        if model.get("status") != "ok":
            skipped.append({"mode": key, "reason": model.get("status", "unknown")})
            # A previously-fitted model for this mode is now unsupported by the
            # current data (e.g. a record's material/thickness was corrected,
            # or the pool grew and no longer clears the gate) — a stale model
            # would otherwise sit in scan_model_by_mat forever and keep being
            # applied to new estimates as if still valid.
            if key in current:
                del current[key]
                removed.append(key)
            continue
        stored = {k: v for k, v in model.items() if k != "status"}
        if current.get(key) != stored:
            logger.info("scan calibration: %s fitted (r2=%.3f, n=%d, total_err=%+.1f%%)",
                        key, model["r2"], model["n_layers"], model["total_err_pct"])
            applied[key] = stored

    # Models are auto-managed when unlocked. A key that is no longer backed by
    # any candidate (records deleted/reclassified/excluded) is stale too.
    for key in list(current):
        if key not in report["candidates"] and key not in removed:
            del current[key]
            removed.append(key)

    if removed:
        logger.info("scan calibration: removed stale model(s) no longer supported: %s", removed)
    if applied or removed:
        current.update(applied)
        row.scan_model_by_mat = current
        row.updated_at = datetime.now(timezone.utc)

    cycle_applied: dict[str, dict] = {}
    cycle_skipped: list[dict] = []
    cycle_removed: list[str] = []
    for key, model in report.get("cycle_candidates", {}).items():
        if model.get("status") != "ok":
            cycle_skipped.append({"mode": key, "reason": model.get("status", "unknown")})
            if key in current_cycles:
                del current_cycles[key]
                cycle_removed.append(key)
            continue
        stored = {field: value for field, value in model.items() if field != "status"}
        if current_cycles.get(key) != stored:
            cycle_applied[key] = stored
    for key in list(current_cycles):
        if key not in report.get("cycle_candidates", {}) and key not in cycle_removed:
            del current_cycles[key]
            cycle_removed.append(key)
    if cycle_applied or cycle_removed:
        current_cycles.update(cycle_applied)
        row.layer_cycle_model_by_mode = current_cycles
        row.updated_at = datetime.now(timezone.utc)

    return {
        "applied": applied,
        "skipped": skipped,
        "removed": removed,
        "cycle_applied": cycle_applied,
        "cycle_skipped": cycle_skipped,
        "cycle_removed": cycle_removed,
        "locked": False,
    }


__all__ = [
    "scan_calibration_report",
    "recalibrate_scan_and_apply",
    "session_burn_by_layer",
    "session_layer_overhead_ms_by_layer",
    "session_layer_cycles_ms_by_layer",
    "_layer_overhead_ms_by_layer",
    "_layer_cycles_ms_by_layer",
    "_fit_layer_cycle_model",
    "MIN_LAYERS_FOR_FIT",
    "MIN_PRINTS_FOR_FIT",
    "MIN_FIT_R2",
    "MIN_FIT_R2_FLOOR",
    "MAX_TOTAL_ERR_PCT",
    "TIGHT_TOTAL_ERR_PCT",
    "MAX_CV_MEDIAN_TOTAL_ERR_PCT",
    "MAX_CV_WORST_TOTAL_ERR_PCT",
    "MIN_CYCLE_BRANCH_LAYERS",
    "MIN_CYCLE_BRANCH_PRINTS",
]
