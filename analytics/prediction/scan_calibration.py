"""Fit scan-time models from real per-layer burn_ms in printer logs (Level B).

Ground truth: ``*_time.log`` records, per physical layer, the machine's own
measured ``burn_ms`` (scan duration). Predictor: the co-hatched per-layer
geometry series the estimate stored in the prediction snapshot
(``metadata_json["prediction"]["scan_geometry"]``) at estimate time — pairing
them needs no re-slicing, just interpolation of the stored series at each
layer's height.

Model: per-layer ``burn_seconds ≈ Σ beta_k · g_k / laser_count + intercept``
over ``layer_engine.GEOMETRY_FEATURES``, fitted with non-negative least squares
(physics forbids negative time per unit length). Validated on this shop's real
builds through the production engine: per-layer R² 0.941 (n=1983, 72-body
plate) and R² 0.26 (n=949, low-variance plate), with in-sample totals
reconstructed to −0.0% and +0.6% where the preset-physics path was −54% and
−89%. Block cross-validation puts the realistic error on a FUTURE build of the
same calibrated mode at roughly ±10% (worst boundary folds ±25%).

Two hard-won honesty rules, both from real-data validation:

* The betas are NOT physical speeds. Geometry components are strongly
  collinear (they all grow with cross-section size), so NNLS concentrates
  weight arbitrarily among them. Only the fitted linear MAP is identifiable —
  never report ``1/beta`` as a speed.
* Models do NOT transfer across modes. Fitting on 0.06 mm and predicting
  0.025 mm gave R² < 0 (worse than predicting the mean). Models are therefore
  keyed by exact ``(material, layer_thickness)`` and applied only on an exact
  key match — the estimator falls back to physics presets otherwise.

Robustness to the known multi-day log-splitting bug: a session holding only
part of a print's layers still yields valid (geometry, burn) pairs for the
layers it has. Acceptance nevertheless requires at least two independent
prints and leave-one-print-out validation; an in-sample fit of one build is not
evidence that the model predicts the next build.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from analytics.prediction.accuracy import (
    PRINT_CLASSIFICATIONS,
    calibration_is_excluded,
    iter_linked_prints,
    session_classification,
)
from analytics.prediction.layer_engine import GEOMETRY_FEATURES, LayerGeometrySeries
from analytics.prediction.plate_estimator import scan_model_key
from domain.enums.common import SourceFileFamily
from domain.models.prints import MachineParams

logger = logging.getLogger(__name__)

# Gates a fitted model must clear before it is ever used for predictions.
MIN_LAYERS_FOR_FIT = 150          # enough layers to constrain the fit
MIN_PRINTS_FOR_FIT = 2            # one print can only prove memorisation
MIN_FIT_R2 = 0.6                  # strong per-layer explanatory power
MAX_TOTAL_ERR_PCT = 15.0          # in-sample total must reconstruct within this
# Secondary acceptance channel, from real-data validation: a build whose
# geometry barely varies with height has almost no variance for R² to explain
# (a real 949-layer build fitted R²=0.26 while reconstructing its total to
# +0.6%) — rejecting it would discard a working model. Low-R² fits are accepted
# only when some geometric signal exists AND the total is tight. Pure noise
# fits R²≈0 and still fails.
MIN_FIT_R2_FLOOR = 0.2
TIGHT_TOTAL_ERR_PCT = 5.0
MAX_CV_MEDIAN_TOTAL_ERR_PCT = 25.0
MAX_CV_WORST_TOTAL_ERR_PCT = 35.0
# Plausibility bounds on a single layer's burn reading (ms) — mirrors the
# pour_ms guards in recoat_calibration.
_MIN_BURN_MS, _MAX_BURN_MS = 100.0, 3_600_000.0


def _burn_seconds_by_layer(events: list[Any]) -> dict[int, float]:
    """{layer: burn_seconds} from parsed time_log events, first-wins per layer."""
    out: dict[int, float] = {}
    for event in events:
        event_type = getattr(event, "event_type", None) if not isinstance(event, dict) else event.get("event_type")
        if event_type != "layer_timing_summary":
            continue
        payload = getattr(event, "payload", None) if not isinstance(event, dict) else event.get("payload")
        payload = payload or {}
        layer, burn_ms = payload.get("layer"), payload.get("burn_ms")
        if not isinstance(layer, int) or not isinstance(burn_ms, (int, float)):
            continue
        if not (_MIN_BURN_MS <= burn_ms <= _MAX_BURN_MS):
            continue
        out.setdefault(layer, burn_ms / 1000.0)
    return out


def session_burn_by_layer(session_id: str, db: Session) -> dict[int, float] | None:
    """Real per-layer burn seconds for one session.

    Prefers the stored per-layer conclusions (see layer_timings — they survive
    the file not being on this machine, which is the shared-database case);
    falls back to re-parsing the log for sessions imported before storage.
    """
    from analytics.prediction.layer_timings import stored_timings

    stored = stored_timings(session_id, db)
    if stored:
        return {layer: burn / 1000.0 for layer, (burn, _) in stored.items()}

    from storage.repositories.runtime import RuntimeRepository

    files = RuntimeRepository(db).get_session_files(session_id, rehydrate=True)
    if not files:
        return None
    out: dict[int, float] = {}
    for f in files:
        if f.classification.family != SourceFileFamily.time_log or not f.parse_result:
            continue
        for layer, sec in _burn_seconds_by_layer(f.parse_result.events).items():
            out.setdefault(layer, sec)
    return out or None


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

    X_rows, y = [], []
    for layer, burn_s in sorted(burn.items()):
        z = series.z_min + (layer - 0.5) * thickness
        if not (series.zs[0] <= z <= series.zs[-1]):
            continue
        g = series.at(z)
        X_rows.append([v / max(laser_count, 1) for v in g] + [1.0])
        y.append(burn_s)
    if not X_rows:
        return None
    return X_rows, y


def _fit_beta(groups: list[tuple[list[list[float]], list[float]]]):
    """Fit one equally-per-print weighted NNLS coefficient vector."""
    import numpy as np
    from scipy.optimize import nnls

    X_weighted: list[list[float]] = []
    y_weighted: list[float] = []
    for X_rows, y in groups:
        if not y:
            continue
        sqrt_w = (1.0 / len(y)) ** 0.5
        for row, val in zip(X_rows, y):
            X_weighted.append([v * sqrt_w for v in row])
            y_weighted.append(val * sqrt_w)
    if not X_weighted:
        return None
    try:
        beta, _ = nnls(np.asarray(X_weighted, dtype=float), np.asarray(y_weighted, dtype=float))
    except Exception:
        logger.exception("scan calibration: NNLS failed")
        return None
    return beta


def _fit(groups: list[tuple[list[list[float]], list[float]]]) -> dict[str, Any] | None:
    """NNLS fit with in-sample and leave-one-print-out quality.

    ``groups`` is one (X_rows, y) per source print. A print with 2000 layers
    and a print with 800 layers otherwise get 2000 vs 800 votes in the fit —
    the longer print dominates purely by length, not by how well-measured or
    representative it is. Each print's rows are scaled by ``1/n_layers`` of
    that print before fitting, so every print contributes one equal share
    regardless of length; R² and total_err_pct are still computed against the
    real, unweighted data so they keep reporting genuine fit quality.
    """
    import numpy as np

    X_all: list[list[float]] = []
    y_all: list[float] = []
    for X_rows, y in groups:
        if not y:
            continue
        for row, val in zip(X_rows, y):
            X_all.append(row)
            y_all.append(val)
    if not X_all:
        return None

    X = np.asarray(X_all, dtype=float)
    yv = np.asarray(y_all, dtype=float)
    beta = _fit_beta(groups)
    if beta is None:
        return None
    pred = X @ beta
    ss_res = float(((yv - pred) ** 2).sum())
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    total_err_pct = (float(pred.sum()) - float(yv.sum())) / float(yv.sum()) * 100.0 if yv.sum() else 0.0
    model = {
        "beta": [float(b) for b in beta],
        "features": list(GEOMETRY_FEATURES),
        "r2": round(r2, 4),
        "total_err_pct": round(total_err_pct, 2),
        "n_layers": len(y_all),
        "n_prints": len(groups),
        "fitted_at": datetime.now(timezone.utc).isoformat(),
    }

    cv_errors: list[float] = []
    if len(groups) >= 2:
        for holdout_idx, (X_holdout, y_holdout) in enumerate(groups):
            train = [group for idx, group in enumerate(groups) if idx != holdout_idx]
            fold_beta = _fit_beta(train)
            if fold_beta is None or not y_holdout:
                continue
            fold_pred = np.asarray(X_holdout, dtype=float) @ fold_beta
            actual_total = float(np.asarray(y_holdout, dtype=float).sum())
            if actual_total > 0:
                cv_errors.append((float(fold_pred.sum()) - actual_total) / actual_total * 100.0)
    model["cv_total_errors_pct"] = [round(value, 2) for value in cv_errors]
    model["cv_median_abs_total_err_pct"] = (
        round(statistics.median(abs(value) for value in cv_errors), 2)
        if cv_errors else None
    )
    model["cv_worst_abs_total_err_pct"] = (
        round(max(abs(value) for value in cv_errors), 2) if cv_errors else None
    )
    return model


def _gate(model: dict[str, Any]) -> str | None:
    """Reason this fit must not be used, or None if it passes."""
    if model["n_prints"] < MIN_PRINTS_FOR_FIT:
        return f"too_few_prints ({model['n_prints']} < {MIN_PRINTS_FOR_FIT})"
    if model["n_layers"] < MIN_LAYERS_FOR_FIT:
        return f"too_few_layers ({model['n_layers']} < {MIN_LAYERS_FOR_FIT})"
    if abs(model["total_err_pct"]) > MAX_TOTAL_ERR_PCT:
        return f"total_err ({model['total_err_pct']}% > {MAX_TOTAL_ERR_PCT}%)"
    cv_median = model.get("cv_median_abs_total_err_pct")
    cv_worst = model.get("cv_worst_abs_total_err_pct")
    if cv_median is None or cv_median > MAX_CV_MEDIAN_TOTAL_ERR_PCT:
        return f"cv_median_total_err ({cv_median}% > {MAX_CV_MEDIAN_TOTAL_ERR_PCT}%)"
    if cv_worst is None or cv_worst > MAX_CV_WORST_TOTAL_ERR_PCT:
        return f"cv_worst_total_err ({cv_worst}% > {MAX_CV_WORST_TOTAL_ERR_PCT}%)"
    if model["r2"] >= MIN_FIT_R2:
        return None
    if model["r2"] >= MIN_FIT_R2_FLOOR and abs(model["total_err_pct"]) <= TIGHT_TOTAL_ERR_PCT:
        return None  # low-variance build: weak per-layer signal, tight total
    return f"low_r2 ({model['r2']} < {MIN_FIT_R2})"


def scan_calibration_report(db: Session) -> dict:
    """Collect (geometry, burn) pairs per mode and fit candidate models."""
    rows: list[dict] = []
    # One (X_rows, y) group per contributing print, not a flat pool — _fit
    # weights each print's group equally regardless of its own layer count.
    by_key: dict[str, list[tuple[list[list[float]], list[float], str]]] = defaultdict(list)

    linked = list(iter_linked_prints(db))
    link_counts = Counter(record.session_id for record, _ in linked)
    for record, session in linked:
        snapshot = (record.metadata_json or {}).get("prediction") or {}
        geo = snapshot.get("scan_geometry")
        if not isinstance(geo, dict):
            continue
        if session_classification(session) not in PRINT_CLASSIFICATIONS:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "not_a_print"})
            continue
        if calibration_is_excluded(record, "scan"):
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "manually_excluded"})
            continue
        if link_counts[record.session_id] > 1:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "duplicate_session_link"})
            continue
        burn = session_burn_by_layer(record.session_id, db)
        if not burn:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "no_time_log"})
            continue
        pairs = _pairs_from_record(snapshot, burn)
        if pairs is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "geometry_snapshot_unusable"})
            continue

        material = (snapshot.get("material") or record.material or "—")
        thickness = float(geo["layer_thickness_mm"])
        key = scan_model_key(material, thickness)
        by_key[key].append((pairs[0], pairs[1], record.record_id))
        rows.append({"record_id": record.record_id, "session_id": record.session_id,
                     "used": True, "mode": key, "n_layers": len(pairs[1])})

    candidates: dict[str, dict] = {}
    for key, groups in by_key.items():
        model = _fit([(X_rows, y) for X_rows, y, _ in groups])
        if model is None:
            candidates[key] = {"status": "fit_failed"}
            continue
        model["source_records"] = [rid for _, _, rid in groups]
        reason = _gate(model)
        model["status"] = "ok" if reason is None else f"rejected: {reason}"
        candidates[key] = model

    return {
        "records": rows,
        "candidates": candidates,
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
    report = scan_calibration_report(db)

    row = db.get(MachineParams, 1)
    if row is None:
        return {"applied": {}, "skipped": [], "locked": False, "reason": "no machine params"}
    if row.correction_locked:
        return {"applied": {}, "skipped": [], "locked": True}

    current = dict(row.scan_model_by_mat or {})
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

    return {"applied": applied, "skipped": skipped, "removed": removed, "locked": False}


__all__ = [
    "scan_calibration_report",
    "recalibrate_scan_and_apply",
    "session_burn_by_layer",
    "MIN_LAYERS_FOR_FIT",
    "MIN_PRINTS_FOR_FIT",
    "MIN_FIT_R2",
    "MIN_FIT_R2_FLOOR",
    "MAX_TOTAL_ERR_PCT",
    "TIGHT_TOTAL_ERR_PCT",
    "MAX_CV_MEDIAN_TOTAL_ERR_PCT",
    "MAX_CV_WORST_TOTAL_ERR_PCT",
]
