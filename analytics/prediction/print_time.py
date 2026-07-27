"""Print-time estimation for SLM builds — PySLM vector engine only.

The estimate hatches a sample of layers with PySLM and times the **real scan
vectors** (hatch + contour + laser-off jumps and delays), then scales to the
true layer count and divides across lasers. There is no longer a fast "Excel"
area formula: it systematically under-predicted (ignored jumps, supports and
everything outside the single sliced part) and only misled the operator. When
PySLM cannot build the trajectories for a file we raise ``EstimationError``
rather than return a wrong number.

Accuracy is closed-loop: the raw geometric estimate (``raw_print_hours``) is
multiplied by a calibration factor learned per material from predicted-vs-actual
history (``time_correction_by_mat`` → falls back to the global
``time_correction_factor``). See ``analytics.prediction.accuracy``.

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
from dataclasses import dataclass, field

from analytics.prediction.stl_slicer import EstimationError, SliceResult

logger = logging.getLogger(__name__)

# Откалибровано по реальным печатям M-350; используется только как фоллбэк,
# когда recoat_time_ms не задан в параметрах машины.
_DEFAULT_RECOAT_MS = 9500

# Фоллбэки параметров сканера для векторного расчёта, когда они не заданы
# в параметрах машины (их следует задать через UI для точности).
_DEFAULT_JUMP_SPEED_MM_S = 5000.0
_DEFAULT_JUMP_DELAY_MS = 0.0


@dataclass
class PrintTimeEstimate:
    scan_hours: float           # laser-on time, divided across lasers (after correction)
    recoat_hours: float         # powder recoating, sequential regardless of lasers
    print_hours: float          # scan + recoat = machine busy time (after correction)
    total_days: float           # continuous printing, 24 h/day
    method: str                 # "pyslm"
    raw_print_hours: float = 0.0    # geometric estimate BEFORE calibration (for accuracy loop)
    correction_factor: float = 1.0  # calibration multiplier applied (per-material → global)
    breakdown: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


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


def resolve_correction_factor(params: dict, material: str) -> float:
    """Calibration multiplier for this material: per-material → global → 1.0."""
    by_mat = params.get("time_correction_by_mat") or {}
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
    ``slices`` already carries the layer count / height for that orientation.

    Raises ``EstimationError`` when PySLM cannot build trajectories for the
    file — we never silently fall back to a less accurate formula.
    """
    hatch_speed, contour_speed, hatch_distance, laser_count = _resolve_params(params, material)
    recoat_ms, recoat_source = resolve_recoat_ms(params, material)
    warnings: list[str] = list(slices.warnings)

    if not contour_speed:
        warnings.append("Скорость контуров не задана — контуры не учтены.")
    if stl_bytes is None:
        raise EstimationError(
            "Точный расчёт недоступен: нет геометрии STL для построения траекторий."
        )

    # Real vectors per sampled layer over the whole height, integrated — the
    # old path hatched 10 sample sections and scaled the MEAN by layer count,
    # which averaged away geometry variation with height.
    from analytics.prediction.layer_engine import (
        compute_layer_series,
        resolve_scan_model,
        scan_seconds_from_model,
    )

    try:
        import trimesh

        mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
        series = compute_layer_series([mesh], hatch_distance, slices.layer_thickness_mm)
    except EstimationError:
        raise
    except Exception as exc:
        raise EstimationError(
            "Точный расчёт недоступен: не удалось построить траектории PySLM "
            f"({exc}). Проверьте STL (геометрия, ориентация, масштаб)."
        )
    totals = series.totals(slices.layer_thickness_mm)

    fitted = resolve_scan_model(params, material, slices.layer_thickness_mm)
    scan_source = "physics"
    if fitted is not None:
        scan_seconds = scan_seconds_from_model(
            totals, slices.layer_count, laser_count, fitted,
        )
        scan_source = "fitted"
    else:
        jump_speed = params.get("jump_speed_mm_s") or _DEFAULT_JUMP_SPEED_MM_S
        jump_delay_s = (params.get("jump_delay_ms") or _DEFAULT_JUMP_DELAY_MS) / 1000.0
        scan_seconds = totals["hatch_mm"] / hatch_speed
        if contour_speed > 0:
            scan_seconds += totals["contour_mm"] / contour_speed
        scan_seconds += totals["open_mm"] / hatch_speed
        scan_seconds += totals["jump_mm"] / jump_speed + totals["n_jumps"] * jump_delay_s
        scan_seconds /= laser_count
        if not params.get("jump_speed_mm_s"):
            warnings.append("Скорость перескока не задана — взято значение по умолчанию.")

    recoat_seconds = slices.layer_count * recoat_ms / 1000.0
    if recoat_source == "default":
        warnings.append(
            f"Время нанесения слоя не задано и не откалибровано по логам — используется "
            f"значение по умолчанию {recoat_ms / 1000:.1f} с/слой."
        )

    raw_scan_hours = scan_seconds / 3600.0
    raw_recoat_hours = recoat_seconds / 3600.0
    raw_print_hours = raw_scan_hours + raw_recoat_hours

    # Calibration: the physics path scales by the per-material factor learned
    # from predicted-vs-actual history. The fitted path is already absolute
    # (trained on real burn seconds) — a factor on top would double-correct.
    factor = 1.0 if scan_source == "fitted" else resolve_correction_factor(params, material)
    scan_hours = raw_scan_hours * factor
    recoat_hours = raw_recoat_hours * factor
    print_hours = raw_print_hours * factor

    return PrintTimeEstimate(
        scan_hours=scan_hours,
        recoat_hours=recoat_hours,
        print_hours=print_hours,
        total_days=print_hours / 24.0,
        method="cohatch" + ("+fitted" if scan_source == "fitted" else ""),
        raw_print_hours=raw_print_hours,
        correction_factor=factor,
        breakdown={
            "build_axis": "Z",  # построение вдоль Z, плита = z_min
            "layer_count": slices.layer_count,
            "height_mm": round(slices.height_mm, 2),
            "avg_section_area_mm2": round(slices.avg_area_mm2, 1),
            "avg_section_perimeter_mm": round(slices.avg_perimeter_mm, 1),
            "hatch_speed": hatch_speed,
            "contour_speed": contour_speed,
            "hatch_distance_mm": hatch_distance,
            "laser_count": laser_count,
            "material": material,
            "correction_factor": factor,
            "recoat_time_ms": round(recoat_ms, 1),
            "recoat_time_source": recoat_source,
            "scan_source": scan_source,
        },
        warnings=warnings,
    )


__all__ = [
    "PrintTimeEstimate", "estimate_print_time", "EstimationError",
    "resolve_correction_factor", "resolve_recoat_ms",
]
