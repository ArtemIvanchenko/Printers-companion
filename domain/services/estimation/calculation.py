"""Local model download, plate calculation and snapshot assembly; no HTTP."""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from domain.services.estimation.contracts import EstimateError
from domain.services.estimation.inputs import (
    _geometry_quality,
    prediction_input_hash,
    geometry_fingerprint,
)
from storage.object_store.minio_client import ObjectStore

if TYPE_CHECKING:
    from analytics.prediction.calibration_inputs import CalibrationInputs

logger = logging.getLogger(__name__)


def combined_prediction(
    parts: list[tuple[str, bytes | Path]],
    supports: list[tuple[str, bytes | Path]],
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
        cost_est = estimate_cost(
            combined_slices,
            params,
            material,
            est.as_print_time_estimate(),
            powder_cost_override=powder_cost,
        )
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

    prediction = est.prediction.to_dict() if est.prediction else None
    return {
        "available": True,
        "n_parts": sum(1 for b in est.bodies if b.kind == "part"),
        "n_support_bodies": sum(1 for b in est.bodies if b.kind == "support"),
        "method": est.method,
        "build_axis": "Z",
        "build_origin_z_mm": round(est.build_origin_z_mm, 3),
        "build_origin_source": est.build_origin_source,
        "layer_count": est.layer_count,
        "height_mm": round(est.height_mm, 2),
        "print_hours": round(est.print_hours, 3),
        "raw_scan_hours": round(est.raw_scan_hours, 3),
        "raw_recoat_hours": round(est.raw_recoat_hours, 3),
        "raw_print_hours": round(est.raw_print_hours, 3),
        "correction_factor": round(est.correction_factor, 3),
        "scan_hours": round(est.scan_hours, 3),
        "recoat_hours": round(est.recoat_hours, 3),
        "cost_total_rub": cost_est.total_rub,
        "scan_source": est.scan_source,
        "recoat_time_ms": round(est.recoat_time_ms, 1),
        "recoat_time_source": est.recoat_time_source,
        "layer_overhead_ms": (
            round(est.layer_overhead_ms, 1) if est.layer_overhead_ms is not None else None
        ),
        "layer_overhead_source": est.layer_overhead_source,
        "layer_overhead_hours": round(est.layer_overhead_hours, 3),
        "layer_overhead_n_prints": est.layer_overhead_n_prints,
        "layer_overhead_n_layers": est.layer_overhead_n_layers,
        "layer_cycle_n_geometries": est.layer_cycle_n_geometries,
        "layer_cycle_model_version": est.layer_cycle_model_version,
        "minimum_layer_cycle_ms": (
            round(est.minimum_layer_cycle_ms, 1) if est.minimum_layer_cycle_ms is not None else None
        ),
        "minimum_layer_cycle_status": est.minimum_layer_cycle_status,
        "minimum_cycle_active_layers": est.minimum_cycle_active_layers,
        "minimum_cycle_training_active_layers": est.minimum_cycle_training_active_layers,
        "minimum_cycle_training_active_prints": est.minimum_cycle_training_active_prints,
        "machine_cycle_hours": round(est.machine_cycle_hours, 3),
        "laser_count": int(params.get("laser_count") or 1),
        "geometry_totals": {
            name: round(float(value), 1) for name, value in est.geometry_totals.items()
        },
        "geometry_regions": [
            {
                "name": body.name,
                "kind": body.kind,
                "z_min_mm": round(float(body.z_min_mm), 3) if body.z_min_mm is not None else None,
                "z_max_mm": round(float(body.z_max_mm), 3) if body.z_max_mm is not None else None,
                "height_mm": round(float(body.height_mm), 3),
                "scan_share": round(float(body.scan_share), 6),
                "active_z_intervals_mm": [
                    [round(float(low), 3), round(float(high), 3)]
                    for low, high in body.active_z_intervals_mm
                ],
                "xy_bounds_mm": {
                    "x": [round(float(body.x_min_mm), 3), round(float(body.x_max_mm), 3)],
                    "y": [round(float(body.y_min_mm), 3), round(float(body.y_max_mm), 3)],
                }
                if None
                not in (
                    body.x_min_mm,
                    body.x_max_mm,
                    body.y_min_mm,
                    body.y_max_mm,
                )
                else None,
            }
            for body in est.bodies
        ],
        "prediction": prediction,
        "cost_prediction": cost_est.prediction.to_dict() if cost_est.prediction else None,
        "warnings": est.warnings + cost_warnings,
        # Per-layer geometry series — persisted into the snapshot so scan
        # calibration can later pair it with real burn_ms without re-slicing.
        "scan_geometry": {
            **est.geometry_series.to_snapshot(),
            "layer_thickness_mm": float(params["layer_thickness_mm"]),
            "laser_count": int(params.get("laser_count") or 1),
        }
        if est.geometry_series is not None
        else None,
    }


def _calibration_mismatch_warning(
    point_hours: float,
    interval: tuple[float, float],
    material: str,
    layer_thickness_mm: float,
) -> str | None:
    """Warn when history contradicts the uncalibrated physics point.

    An out-of-range correction factor is deliberately not auto-applied, but
    silently returning the raw point would still make a known-bad number look
    authoritative.  Keep the point for auditability and surface the empirical
    interval as the operator-facing safety signal.
    """
    low, high = interval
    if low <= point_hours <= high:
        return None
    mode = f"{material.strip().lower()}@{layer_thickness_mm:.3f}"
    return (
        f"История режима {mode} не подтверждает физическую точку {point_hours:.2f} ч: "
        f"она вне эмпирического интервала {low:.2f}–{high:.2f} ч. "
        "Используйте интервал и проверьте параметры сканирования/полноту геометрии."
    )


def calculate_prediction_snapshot(
    prepared: dict[str, Any],
    *,
    geometry_cache: Any | None = None,
    computed_by: str | None = None,
    object_store_factory=None,
    plate_calculator=None,
) -> dict:
    """Download models and calculate locally without requiring an open DB tx."""
    from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION

    record = prepared["record"]
    platform_files = prepared["platform_files"]
    material = prepared["material"]
    params = prepared["params"]
    from analytics.prediction.layer_engine import scan_geometry_options

    geometry_options = scan_geometry_options(params)

    store = (object_store_factory or ObjectStore)()
    with tempfile.TemporaryDirectory(prefix="printer-estimator-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        parts: list[tuple[str, Path]] = []
        supports: list[tuple[str, Path]] = []
        for index, f in enumerate(platform_files):
            bucket, _, object_name = f["object_uri"].removeprefix("s3://").partition("/")
            local_name = f"{index:04d}_{Path(f['file_name']).name}"
            local_path = store.download_file(
                bucket,
                object_name,
                temporary_root / local_name,
                expected_sha256=f.get("checksum") or None,
            )
            if local_path is None:
                raise EstimateError(
                    "storage_unavailable",
                    f"STL недоступен или повреждён в хранилище: {f['file_name']}",
                )
            if f["file_type"] == "stl_supports":
                supports.append((f["file_name"], local_path))
            else:
                parts.append((f["file_name"], local_path))
        n_supports = len(supports)

        # Keep the temporary files alive until trimesh has loaded every body
        # and the joint plate calculation is complete. Only mesh arrays, not
        # the original multi-hundred-megabyte blobs, remain in memory.
        result = (plate_calculator or combined_prediction)(
            parts,
            supports,
            material,
            params,
            prepared["powder_cost"],
            geometry_cache=geometry_cache,
        )
    if not result.get("available"):
        raise EstimateError("invalid_inputs", f"Расчёт недоступен: {result.get('reason')}")

    time_prediction = result.get("prediction")
    cost_prediction = result.get("cost_prediction")
    geometry_quality = dict(_geometry_quality(record))
    if not supports and not geometry_quality:
        geometry_quality = {
            "status": "lower_bound",
            "note": "Support-STL не приложены; время и геометрическая привязка могут быть нижней границей.",
        }
    geometry_quality["build_origin_source"] = result.get("build_origin_source")
    geometry_quality["build_origin_z_mm"] = result.get("build_origin_z_mm")
    quality_status = geometry_quality.get("status") or "standard"
    quality_warning = None
    if quality_status == "lower_bound":
        quality_warning = geometry_quality.get("note") or (
            "Оценка является нижней границей: часть печатаемой геометрии отсутствует."
        )
    prediction_warnings = list((time_prediction or {}).get("warnings", []))
    if quality_warning and quality_warning not in prediction_warnings:
        prediction_warnings.append(str(quality_warning))

    from analytics.prediction.layer_engine import scan_model_key

    machine_cycle_hours = float(result.get("machine_cycle_hours") or result["print_hours"])
    mode_key = scan_model_key(material, float(params["layer_thickness_mm"]))
    machine_mode_key = (
        f"{prepared.get('printer_id') or 'configured-machine'}|{mode_key}|"
        f"lasers={int(result.get('laser_count') or params.get('laser_count') or 1)}"
    )

    from analytics.log_insights.geometry import scan_reference
    from analytics.prediction.scan_scope import scan_scope
    from core.versioning.provenance import stable_hash

    snapshot: dict = {
        "estimated_at": datetime.now(timezone.utc).isoformat(),
        "input_revision": record["revision"],
        "input_hash": prediction_input_hash(prepared),
        "geometry_fingerprint": geometry_fingerprint(platform_files),
        "computed_by": computed_by,
        "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "n_parts": len(parts),
        "n_supports": n_supports,
        "material": material,
        "printer_id": prepared.get("printer_id"),
        "machine_scope": "printer" if prepared.get("printer_id") else "single_configured_machine",
        "mode_key": mode_key,
        "machine_mode_key": machine_mode_key,
        # Preserve effective inputs for reproducibility. A configured value is
        # not proof of the actual machine strategy or a decoded Monitor100 cell.
        "layer_thickness_mm": params.get("layer_thickness_mm"),
        "hatch_distance_mm": params.get("hatch_distance_mm"),
        "laser_count": result.get("laser_count", params.get("laser_count")),
        "method": result["method"],
        "build_axis": result.get("build_axis", "Z"),
        "build_origin_z_mm": result.get("build_origin_z_mm"),
        "build_origin_source": result.get("build_origin_source"),
        "layer_count": result.get("layer_count"),
        "print_hours": result["print_hours"],
        # Backward-compatible ``print_hours`` remains scan + recoat. The full
        # machine cycle adds the separately calibrated make-layer residual.
        "machine_cycle_hours": machine_cycle_hours,
        "machine_hours": machine_cycle_hours,
        # raw (uncorrected) hours feed the calibration loop, so the learned
        # factor stays absolute and never compounds on itself.
        "raw_print_hours": result.get("raw_print_hours", result["print_hours"]),
        "raw_scan_hours": result.get("raw_scan_hours"),
        "raw_recoat_hours": result.get("raw_recoat_hours"),
        "scan_hours": result.get("scan_hours"),
        "recoat_hours": result.get("recoat_hours"),
        "recoat_time_ms": result.get("recoat_time_ms"),
        "recoat_time_source": result.get("recoat_time_source"),
        "layer_overhead_ms": result.get("layer_overhead_ms"),
        "layer_overhead_source": result.get("layer_overhead_source"),
        "layer_overhead_hours": result.get("layer_overhead_hours", 0.0),
        "layer_overhead_n_prints": result.get("layer_overhead_n_prints", 0),
        "layer_overhead_n_layers": result.get("layer_overhead_n_layers", 0),
        "layer_cycle_n_geometries": result.get("layer_cycle_n_geometries", 0),
        "layer_cycle_model_version": result.get("layer_cycle_model_version"),
        "minimum_layer_cycle_ms": result.get("minimum_layer_cycle_ms"),
        "minimum_layer_cycle_status": result.get("minimum_layer_cycle_status"),
        "minimum_cycle_active_layers": result.get("minimum_cycle_active_layers", 0),
        "minimum_cycle_training_active_layers": result.get(
            "minimum_cycle_training_active_layers",
            0,
        ),
        "minimum_cycle_training_active_prints": result.get(
            "minimum_cycle_training_active_prints",
            0,
        ),
        "correction_factor": result.get("correction_factor", 1.0),
        "scan_source": result.get("scan_source", "physics"),
        "scan_calibration_scope": scan_scope(params, material, params.get("layer_thickness_mm")),
        "scan_timing_reference": scan_reference(
            params,
            material,
            float(params["layer_thickness_mm"]),
            float(result.get("correction_factor") or 1.0),
        ),
        "process_profile_fingerprint": stable_hash(
            {
                key: params.get(key)
                for key in (
                    "hatch_speed_mm_s",
                    "hatch_speeds_by_mat",
                    "contour_speed_mm_s",
                    "support_speed_mm_s",
                    "jump_speed_mm_s",
                    "jump_delay_ms",
                    "hatch_distance_mm",
                    "layer_thickness_mm",
                    "laser_count",
                    "contours_enabled",
                    "hatch_angle_deg",
                )
            }
        ),
        "cost_total_rub": result["cost_total_rub"],
        "scan_geometry": result.get("scan_geometry"),
        "geometry_totals": result.get("geometry_totals") or {},
        "geometry_regions": result.get("geometry_regions") or [],
        "calculation_inputs": {
            # Configuration origin only; not a firmware-schema confirmation.
            "parameter_sources": prepared.get("parameter_sources") or {},
            "hatch_speed_mm_s": (params.get("hatch_speeds_by_mat") or {}).get(material)
            or params.get("hatch_speed_mm_s"),
            "printer_id": prepared.get("printer_id"),
            "machine_scope": "printer"
            if prepared.get("printer_id")
            else "single_configured_machine",
            "material": material,
            "layer_thickness_mm": params.get("layer_thickness_mm"),
            "hatch_distance_mm": params.get("hatch_distance_mm"),
            "scan_geometry_options": geometry_options,
            "laser_count": result.get("laser_count", params.get("laser_count")),
            "layer_overhead_ms": result.get("layer_overhead_ms"),
            "minimum_layer_cycle_ms": result.get("minimum_layer_cycle_ms"),
            "geometry_body_count": len(parts) + n_supports,
            "geometry_layer_count": result.get("layer_count"),
            "machine_mode_key": machine_mode_key,
        },
        "time_breakdown": {
            "scan_hours": result.get("scan_hours"),
            "recoat_hours": result.get("recoat_hours"),
            "layer_overhead_hours": result.get("layer_overhead_hours", 0.0),
            "machine_hours": machine_cycle_hours,
            "scan_source": result.get("scan_source", "physics"),
            "recoat_time_ms_per_layer": result.get("recoat_time_ms"),
            "recoat_source": result.get("recoat_time_source"),
            "layer_overhead_source": result.get("layer_overhead_source"),
            "layer_overhead_n_prints": result.get("layer_overhead_n_prints", 0),
            "layer_overhead_n_layers": result.get("layer_overhead_n_layers", 0),
            "layer_cycle_n_geometries": result.get("layer_cycle_n_geometries", 0),
            "minimum_layer_cycle_ms": result.get("minimum_layer_cycle_ms"),
            "minimum_cycle_active_layers": result.get("minimum_cycle_active_layers", 0),
        },
        # Flat, additive fields from the unified prediction contract — kept
        # flat (not nested under a "prediction" key) so they don't collide
        # with this whole snapshot already being metadata_json["prediction"].
        "prediction_source": (time_prediction or {}).get("source"),
        "prediction_interval": (time_prediction or {}).get("interval"),
        "prediction_warnings": prediction_warnings,
        "prediction_explanation": (time_prediction or {}).get("explanation"),
        "cost_prediction_warnings": (cost_prediction or {}).get("warnings", []),
        "estimate_quality": quality_status,
        "geometry_quality": geometry_quality or None,
    }
    from analytics.prediction.input_quality import INPUT_REASON_RU, geometry_input_issues

    issues = geometry_input_issues(record.get("metadata_json") or {}, snapshot)
    snapshot["geometry_input_issues"] = issues
    if issues:
        snapshot["prediction_interval"] = None
    if issues and quality_status == "standard":
        snapshot["estimate_quality"] = "unconfirmed"
    for issue in issues:
        warning = INPUT_REASON_RU[issue]
        if warning not in prediction_warnings:
            prediction_warnings.append(warning)
    return snapshot


def needs_prediction_interval(snapshot: dict) -> bool:
    """Whether a detached historical sample could refine this estimate."""
    from analytics.prediction.input_quality import geometry_input_issues

    if geometry_input_issues({}, snapshot):
        return False
    if snapshot.get("prediction_source") not in ("calculated", "calibrated"):
        return False
    if snapshot.get("layer_overhead_ms") is not None:
        # calibration_interval_hours is defined for scan+recoat. Attaching it
        # to a point that already includes base/floor controller time would mix
        # two different quantities; wait for a full-cycle interval model.
        return False
    return True


def enrich_prediction_interval(snapshot: dict, *, inputs: CalibrationInputs) -> None:
    """Refine an eligible scan+recoat estimate using detached history only."""
    if not needs_prediction_interval(snapshot):
        return
    try:
        from analytics.prediction.accuracy import calibration_interval_hours

        interval = calibration_interval_hours(
            None,
            str(snapshot["material"]),
            float(snapshot["layer_thickness_mm"]),
            float(snapshot.get("raw_scan_hours") or 0.0),
            float(snapshot.get("raw_recoat_hours") or 0.0),
            inputs=inputs,
        )
        if interval is None:
            return
        snapshot["prediction_interval"] = list(interval)
        snapshot["interval_reference"] = {
            "input_fingerprint": inputs.input_fingerprint,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "scan_plus_recoat",
        }
        warning = _calibration_mismatch_warning(
            float(snapshot["print_hours"]),
            interval,
            str(snapshot["material"]),
            float(snapshot["layer_thickness_mm"]),
        )
        if warning:
            warnings = list(snapshot.get("prediction_warnings") or [])
            if warning not in warnings:
                warnings.append(warning)
            snapshot["prediction_warnings"] = warnings
    except Exception:
        logger.exception("prints: calibration interval lookup failed")
