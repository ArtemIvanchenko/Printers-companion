"""Print-time estimation for SLM builds — PySLM vector engine only.

The estimate hatches sample heights with PySLM and projects the geometric scan
vectors (hatch + contour + laser-off jumps and delays) to physical layer
centres, dividing scan time across lasers. There is no longer a fast "Excel"
area formula: it systematically under-predicted (ignored jumps, supports and
everything outside the single sliced part) and only misled the operator. When
PySLM cannot build the trajectories for a file we raise ``EstimationError``
rather than return a wrong number.

Accuracy is closed-loop: only the raw **scan** estimate is multiplied by a
calibration factor learned per material+layer-thickness mode from
predicted-vs-actual history. Recoat is calibrated independently from ``pour_ms``
and must never be multiplied by the scan correction. See
``analytics.prediction.accuracy``.

Recoat time is calibrated separately and *before* that multiplier, because it
is a measured duration, not a scan-time error ratio: ``recoat_time_by_mat`` is
learned per material from real per-layer ``pour_ms`` readings in the printer's
own logs (see ``analytics.prediction.recoat_calibration``), falling back to the
operator-entered ``recoat_time_ms``, then the hardcoded ``_DEFAULT_RECOAT_MS``.

``hatch_speed_mm_s`` is the real laser speed (mm/s); ``hatch_distance_mm`` is
required. Machine parameters come from the machine_params table.
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field

from analytics.prediction.contract import PredictionResult, PredictionSource
from analytics.prediction.stl_slicer import EstimationError, SliceResult

logger = logging.getLogger(__name__)

# Откалибровано по реальным печатям M-350; используется только как фоллбэк,
# когда recoat_time_ms не задан в параметрах машины.
_DEFAULT_RECOAT_MS = 9500

@dataclass
class PrintTimeEstimate:
    scan_hours: float           # laser-on time, divided across lasers (after correction)
    recoat_hours: float         # powder recoating, sequential regardless of lasers
    print_hours: float          # scan + recoat subtotal; not necessarily the full cycle
    total_days: float | None    # full continuous cycle, only when admitted
    method: str                 # "pyslm"
    raw_scan_hours: float = 0.0     # scan before the scan-only correction
    raw_recoat_hours: float = 0.0   # recoat is independently calibrated
    raw_print_hours: float = 0.0    # geometric estimate BEFORE calibration (for accuracy loop)
    correction_factor: float = 1.0  # calibration multiplier applied (per-material → global)
    breakdown: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Unified cross-predictor summary (analytics.prediction.contract). Additive:
    # every field above stays exactly as before for existing callers.
    prediction: PredictionResult | None = None


def _recoat_provenance_note(recoat_source: str) -> str:
    return {
        "calibrated": "по калиброванной медиане из логов",
        "manual": "по значению оператора",
        "default": "по значению по умолчанию",
    }.get(recoat_source, "по значению по умолчанию")


def build_time_prediction(
    *,
    print_hours: float,
    correction_factor: float,
    scan_source: str,
    recoat_source: str,
    fitted_model: dict | None,
    warnings: list[str],
) -> PredictionResult:
    """Unified contract wrapper for a scan+recoat time estimate.

    ``scan_source``/``recoat_source`` are the same provenance strings already
    stored in ``breakdown`` — this only translates them into the shared
    vocabulary. The uncertainty interval is left ``None``: it is filled from
    real predicted-vs-actual history in a separate step (analytics.prediction.accuracy),
    not guessed at here.
    """
    recoat_note = _recoat_provenance_note(recoat_source)
    if scan_source == "fitted":
        source = PredictionSource.MODEL
        n_layers = (fitted_model or {}).get("n_layers")
        r2 = (fitted_model or {}).get("r2")
        explanation = (
            "Скан рассчитан по модели, подогнанной под реальный burn_ms этого режима печати "
            f"(материал+толщина{f', R²={r2}' if r2 is not None else ''}); "
            f"нанесение слоя — {recoat_note}."
        )
        sample_size = n_layers if isinstance(n_layers, int) else None
    elif correction_factor != 1.0:
        source = PredictionSource.CALIBRATED
        explanation = (
            f"Скан рассчитан по заданным скоростям с коэффициентом "
            f"×{correction_factor:.3f} из настроек; "
            f"нанесение слоя — {recoat_note}."
        )
        sample_size = None  # число пар калибровки не передаётся на этот вызов
    else:
        source = PredictionSource.CALCULATED
        explanation = (
            "Скан рассчитан по заданным скоростям без поправочного множителя; "
            f"нанесение слоя — {recoat_note}."
        )
        sample_size = None

    return PredictionResult(
        value=round(print_hours, 3),
        unit="ч",
        source=source,
        sample_size=sample_size,
        warnings=list(warnings),
        explanation=explanation,
    )


def _resolve_params(params: dict, material: str) -> tuple[float, float, float, int]:
    hatch_by_mat = params.get("hatch_speeds_by_mat") or {}
    hatch_speed = hatch_by_mat.get(material) or params.get("hatch_speed_mm_s")
    if not hatch_speed or hatch_speed <= 0:
        raise EstimationError("Не задана скорость штриховки (параметры машины)")
    hatch_distance = params.get("hatch_distance_mm")
    if not hatch_distance or hatch_distance <= 0:
        raise EstimationError("Не задан шаг штриховки hatch_distance_mm (параметры машины)")
    contour_speed = params.get("contour_speed_mm_s") or 0.0
    laser_count = int(params.get("laser_count") or 0)
    if laser_count < 1:
        raise EstimationError("Не задано количество лазеров (параметры машины)")
    return float(hatch_speed), float(contour_speed), float(hatch_distance), laser_count


def resolve_correction_factor(
    params: dict, material: str, layer_thickness_mm: float | None = None,
) -> float:
    """Scan calibration multiplier for a material+thickness mode.

    New learned factors are keyed exactly like scan models (for example
    ``steel@0.060``), because a correction learned at one layer thickness is
    not transferable to another. The material/global lookups are retained as
    a compatibility path for explicitly pinned legacy settings; an unlocked
    recalibration removes those unsafe pooled entries.
    """
    by_mat = params.get("time_correction_by_mat") or {}
    factor = None
    if layer_thickness_mm is not None:
        from analytics.prediction.layer_engine import scan_model_key

        factor = by_mat.get(scan_model_key(material, layer_thickness_mm))
    if factor is None:
        factor = by_mat.get(material)
    if factor is None:
        factor = params.get("time_correction_factor")
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        return 1.0
    return factor if factor > 0 else 1.0


def resolve_recoat_ms(params: dict, material: str) -> tuple[float, str]:
    """Recoat time per layer (ms) and its source: "calibrated" | "manual" | "default".

    Precedence: per-material value learned from real ``pour_ms`` readings in the
    printer's logs (``recoat_time_by_mat`` — see
    ``analytics.prediction.recoat_calibration``) → operator-entered
    ``recoat_time_ms`` → the hardcoded fallback. The source is returned
    alongside the value so callers can be honest with the operator about which
    one produced the number — a calibrated value needs no caveat, the other two
    do.
    """
    by_mat = params.get("recoat_time_by_mat") or {}
    learned = by_mat.get(material)
    try:
        learned = float(learned)
    except (TypeError, ValueError):
        learned = None
    if learned and learned > 0:
        return learned, "calibrated"

    manual = params.get("recoat_time_ms")
    try:
        manual = float(manual)
    except (TypeError, ValueError):
        manual = None
    if manual and manual > 0:
        return manual, "manual"

    return float(_DEFAULT_RECOAT_MS), "default"


def estimate_print_time(
    slices: SliceResult,
    params: dict,
    material: str,
    stl_bytes: bytes | None = None,
) -> PrintTimeEstimate:
    """Estimate machine time from real PySLM scan trajectories.

    Build direction is the STL's +Z axis (the slicer's layer-stacking axis);
    ``slices`` carries the thickness and body height for that orientation; an
    explicitly supplied build origin also determines preceding machine layers.

    Raises ``EstimationError`` when PySLM cannot build trajectories for the
    file — we never silently fall back to a less accurate formula.
    """
    hatch_speed, contour_speed, hatch_distance, laser_count = _resolve_params(params, material)
    recoat_ms, recoat_source = resolve_recoat_ms(params, material)
    warnings: list[str] = list(slices.warnings)

    if stl_bytes is None:
        raise EstimationError(
            "Точный расчёт недоступен: нет геометрии STL для построения траекторий."
        )
    mesh = slices.prepared_mesh
    if mesh is None and slices.was_repaired:
        raise EstimationError(
            "Нет подготовленной починенной сетки; повторите slice_stl для исходного STL."
        )
    build_origin_z = params.get("build_origin_z_mm")
    if build_origin_z is not None:
        try:
            build_origin_z = float(build_origin_z)
        except (TypeError, ValueError) as exc:
            raise EstimationError("build_origin_z_mm должен быть числом") from exc
        if not math.isfinite(build_origin_z):
            raise EstimationError("build_origin_z_mm должен быть конечным числом")

    # Use the same physical-layer projection and scan/cycle math as the plate
    # path. A floor cannot be applied to integrated scan totals after summing.
    from analytics.prediction.layer_engine import (
        _resolve_layer_cycle_model_for_mode,
        compute_layer_series,
        machine_cycle_from_layers,
        physics_scan_seconds_by_layer,
        resolve_scan_model,
        scan_seconds_by_layer_from_model,
        scan_geometry_options,
    )

    try:
        import trimesh

        if mesh is None:
            # Compatibility for manually constructed/legacy SliceResult inputs.
            mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
        geometry_options = scan_geometry_options(params)
        series = compute_layer_series(
            [mesh], hatch_distance, slices.layer_thickness_mm,
            build_origin_z_mm=build_origin_z, **geometry_options,
        )
    except EstimationError:
        raise
    except Exception as exc:
        raise EstimationError(
            "Точный расчёт недоступен: не удалось построить траектории PySLM "
            f"({exc}). Проверьте STL (геометрия, ориентация, масштаб)."
        )
    warnings.extend(series.warnings)
    if build_origin_z is None:
        warnings.append(
            "Начало печати не подтверждено, поэтому слои считаются от минимального "
            f"Z переданной геометрии: {series.z_min:.3f} мм. "
            "Для подтверждённой координаты платформы укажите build_origin_z_mm."
        )
    layer_count = series.layer_count(slices.layer_thickness_mm)
    if geometry_options["contours_enabled"] and not contour_speed:
        contour_speed = hatch_speed
        warnings.append("Скорость контуров не задана — использована скорость штриховки.")

    fitted = resolve_scan_model(params, material, slices.layer_thickness_mm)
    scan_source = "physics"
    if fitted is not None:
        raw_scan_seconds_by_layer = scan_seconds_by_layer_from_model(
            series, slices.layer_thickness_mm, laser_count, fitted,
        )
        scan_source = "fitted"
        warnings.append(
            "Модель прожига соответствует сохранённым настройкам; "
            "это не подтверждение фактически исполненной стратегии слайсера."
        )
    else:
        raw_scan_seconds_by_layer = physics_scan_seconds_by_layer(
            series, slices.layer_thickness_mm, params, material, laser_count,
        )
        if not params.get("jump_speed_mm_s"):
            warnings.append("Скорость перескока не задана — взято значение по умолчанию.")

    recoat_seconds = layer_count * recoat_ms / 1000.0
    if recoat_source == "default":
        warnings.append(
            f"Время нанесения слоя не задано и не откалибровано по логам — используется "
            f"значение по умолчанию {recoat_ms / 1000:.1f} с/слой."
        )

    raw_scan_hours = sum(raw_scan_seconds_by_layer) / 3600.0
    raw_recoat_hours = recoat_seconds / 3600.0
    raw_print_hours = raw_scan_hours + raw_recoat_hours

    # Calibration: the physics path scales by the per-material factor learned
    # from predicted-vs-actual history. The fitted path is already absolute
    # (trained on real burn seconds) — a factor on top would double-correct.
    factor = 1.0 if scan_source == "fitted" else resolve_correction_factor(
        params, material, slices.layer_thickness_mm,
    )
    scan_hours = raw_scan_hours * factor
    recoat_hours = raw_recoat_hours
    print_hours = scan_hours + recoat_hours

    cycle_model, layer_overhead_source = _resolve_layer_cycle_model_for_mode(
        params, material, slices.layer_thickness_mm, fitted,
    )
    if cycle_model is not None and cycle_model.get("minimum_cycle_ms") is None:
        lower = cycle_model.get("minimum_applicable_component_ms")
        if (not isinstance(lower, (int, float)) or isinstance(lower, bool)
                or not math.isfinite(lower) or lower < 0
                or any(scan * factor * 1000 + recoat_ms < lower
                       for scan in raw_scan_seconds_by_layer)):
            cycle_model, layer_overhead_source = None, "outside_calibrated_range"
            warnings.append(
                "Есть слои короче проверенной области калибровки; "
                "неизвестное минимальное время цикла не заменено постоянной добавкой."
            )
    cycle_seconds, _, minimum_cycle_active_layers = machine_cycle_from_layers(
        raw_scan_seconds_by_layer, scan_correction_factor=factor,
        recoat_ms=recoat_ms, cycle_model=cycle_model,
    )
    machine_cycle_hours = cycle_seconds / 3600.0 if cycle_model is not None else None
    if cycle_model is None:
        warnings.append(
            "Нормальный полный цикл слоя пока не откалиброван по make_layer_ms; "
            "прогноз содержит только прожиг и нанесение, полный цикл неизвестен."
        )
    prediction = build_time_prediction(
        print_hours=machine_cycle_hours if machine_cycle_hours is not None else print_hours,
        correction_factor=factor, scan_source=scan_source, recoat_source=recoat_source,
        fitted_model=fitted if scan_source == "fitted" else None, warnings=warnings,
    )
    prediction.explanation += (
        " Полный нормальный цикл учитывает откалиброванную модель контроллера в её проверенной области."
        if machine_cycle_hours is not None else
        " Оценена сумма прожига и нанесения; полный нормальный цикл неизвестен."
    )

    return PrintTimeEstimate(
        scan_hours=scan_hours,
        recoat_hours=recoat_hours,
        print_hours=print_hours,
        total_days=machine_cycle_hours / 24.0 if machine_cycle_hours is not None else None,
        method="cohatch" + ("+fitted" if scan_source == "fitted" else ""),
        raw_scan_hours=raw_scan_hours,
        raw_recoat_hours=raw_recoat_hours,
        raw_print_hours=raw_print_hours,
        correction_factor=factor,
        breakdown={
            "build_axis": "Z",
            "build_origin_z_mm": series.z_min,
            "build_origin_source": (
                "explicit" if build_origin_z is not None else "minimum_supplied_geometry_z"
            ),
            "layer_count": layer_count,
            "height_mm": round(slices.height_mm, 2),
            "avg_section_area_mm2": round(slices.avg_area_mm2, 1),
            "avg_section_perimeter_mm": round(slices.avg_perimeter_mm, 1),
            "hatch_speed": hatch_speed,
            "contour_speed": contour_speed,
            "hatch_distance_mm": hatch_distance,
            "scan_geometry_options": geometry_options,
            "laser_count": laser_count,
            "material": material,
            "correction_factor": factor,
            "recoat_time_ms": round(recoat_ms, 1),
            "recoat_time_source": recoat_source,
            "scan_source": scan_source,
            "print_hours": print_hours,
            "machine_cycle_hours": machine_cycle_hours,
            "prediction_time_scope": "machine_cycle" if cycle_model is not None else "burn_plus_pour",
            "layer_overhead_source": layer_overhead_source,
            "layer_overhead_ms": cycle_model["base_overhead_ms"] if cycle_model is not None else None,
            "layer_cycle_model_version": (cycle_model or {}).get("version"),
            "minimum_layer_cycle_ms": (cycle_model or {}).get("minimum_cycle_ms"),
            "minimum_layer_cycle_status": (cycle_model or {}).get("minimum_cycle_status", "unavailable"),
            "minimum_cycle_active_layers": minimum_cycle_active_layers,
        },
        warnings=warnings,
        prediction=prediction,
    )


__all__ = [
    "PrintTimeEstimate", "estimate_print_time", "EstimationError",
    "resolve_correction_factor", "resolve_recoat_ms", "build_time_prediction",
]
