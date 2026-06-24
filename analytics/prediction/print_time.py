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

``hatch_speed_mm_s`` is the real laser speed (mm/s); ``hatch_distance_mm`` is
required. Machine parameters come from the machine_params table; the only
in-code fallback is the recoat time when ``recoat_time_ms`` is unset
(``_DEFAULT_RECOAT_MS``).
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field

from analytics.prediction.stl_slicer import SliceResult

logger = logging.getLogger(__name__)

# Откалибровано по реальным печатям M-350; используется только как фоллбэк,
# когда recoat_time_ms не задан в параметрах машины.
_DEFAULT_RECOAT_MS = 9500

# Сколько сэмпл-сечений хэтчится по-настоящему в режиме «точно»
_PYSLM_SAMPLE_SECTIONS = 10

# Фоллбэки параметров сканера для векторного расчёта, когда они не заданы
# в параметрах машины (их следует задать через UI для точности).
_DEFAULT_JUMP_SPEED_MM_S = 5000.0
_DEFAULT_JUMP_DELAY_MS = 0.0


class EstimationError(ValueError):
    """Required machine parameter is missing."""


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


def _pyslm_layer_metrics(
    stl_bytes: bytes, slices: SliceResult, hatch_distance: float,
) -> tuple[float, float, float, float] | None:
    """Hatch a sample of layers with PySLM; return the mean per-layer scan
    geometry as ``(hatch_len, contour_len, jump_len, n_jumps)`` in millimetres.

    ``jump_len`` is the laser-off travel between consecutive hatch vectors and
    ``n_jumps`` their count — these are what the fast area formula omits.
    Returns None when PySLM is unavailable or hatching fails.
    """
    try:
        import numpy as np
        import pyslm
        import pyslm.analysis
        from pyslm import hatching as slm_hatching
    except Exception:
        return None

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tmp:
            tmp.write(stl_bytes)
            tmp_path = tmp.name

        part = pyslm.Part("estimate")
        part.setGeometry(tmp_path)
        part.dropToPlatform()

        hatcher = slm_hatching.Hatcher()
        hatcher.hatchDistance = hatch_distance
        hatcher.hatchAngle = 67.0
        hatcher.volumeOffsetHatch = 0.08
        hatcher.spotCompensation = 0.06
        hatcher.numInnerContours = 1
        hatcher.numOuterContours = 1

        n = len(slices.section_zs)
        if n == 0:
            return None
        stride = max(n // _PYSLM_SAMPLE_SECTIONS, 1)
        sample_idx = list(range(0, n, stride))

        hatch_sum = contour_sum = jump_sum = 0.0
        njump_sum = 0
        count = 0
        z_base = slices.section_zs[0] - slices.layer_thickness_mm / 2.0
        for i in sample_idx:
            z = slices.section_zs[i] - z_base  # part dropped to platform → z from 0
            geom_slice = part.getVectorSlice(z)
            if not geom_slice:
                continue
            layer = hatcher.hatch(geom_slice)
            for geom in layer.geometry:
                length = pyslm.analysis.getLayerGeometryPathLength(geom)
                if isinstance(geom, pyslm.geometry.ContourGeometry):
                    contour_sum += length
                else:
                    hatch_sum += length
                    # jumps: end of hatch line i -> start of line i+1
                    co = np.asarray(geom.coords)
                    if len(co) >= 4:
                        ends = co[1::2]
                        starts = co[2::2]
                        m = min(len(ends) - 1, len(starts))
                        if m > 0:
                            jump_sum += float(np.linalg.norm(ends[:m] - starts[:m], axis=1).sum())
                            njump_sum += m
            count += 1

        if count == 0:
            return None
        return hatch_sum / count, contour_sum / count, jump_sum / count, njump_sum / count
    except Exception:
        logger.exception("pyslm hatching failed")
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
    recoat_ms = params.get("recoat_time_ms")
    warnings: list[str] = list(slices.warnings)

    if not contour_speed:
        warnings.append("Скорость контуров не задана — контуры не учтены.")

    metrics = _pyslm_layer_metrics(stl_bytes, slices, hatch_distance) if stl_bytes is not None else None
    if metrics is None:
        raise EstimationError(
            "Точный расчёт недоступен: не удалось построить траектории PySLM. "
            "Проверьте STL (геометрия, ориентация, масштаб)."
        )

    hatch_len, contour_len, jump_len, n_jumps = metrics
    jump_speed = params.get("jump_speed_mm_s") or _DEFAULT_JUMP_SPEED_MM_S
    jump_delay_s = (params.get("jump_delay_ms") or _DEFAULT_JUMP_DELAY_MS) / 1000.0
    # Время слоя по реальным векторам: прожиг + контур + перескоки + задержки
    layer_seconds = hatch_len / hatch_speed
    if contour_speed > 0:
        layer_seconds += contour_len / contour_speed
    layer_seconds += jump_len / jump_speed + n_jumps * jump_delay_s
    scan_seconds = layer_seconds * slices.layer_count / laser_count
    if not params.get("jump_speed_mm_s"):
        warnings.append("Скорость перескока не задана — взято значение по умолчанию.")

    if recoat_ms:
        recoat_seconds = slices.layer_count * float(recoat_ms) / 1000.0
    else:
        recoat_seconds = slices.layer_count * _DEFAULT_RECOAT_MS / 1000.0
        warnings.append(
            f"Время нанесения слоя не задано — используется откалиброванное значение "
            f"{_DEFAULT_RECOAT_MS / 1000:.1f} с/слой."
        )

    raw_scan_hours = scan_seconds / 3600.0
    raw_recoat_hours = recoat_seconds / 3600.0
    raw_print_hours = raw_scan_hours + raw_recoat_hours

    # Calibration: scale the whole estimate by the per-material factor learned
    # from predicted-vs-actual history (so total print_hours tracks reality).
    factor = resolve_correction_factor(params, material)
    scan_hours = raw_scan_hours * factor
    recoat_hours = raw_recoat_hours * factor
    print_hours = raw_print_hours * factor

    return PrintTimeEstimate(
        scan_hours=scan_hours,
        recoat_hours=recoat_hours,
        print_hours=print_hours,
        total_days=print_hours / 24.0,
        method="pyslm",
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
        },
        warnings=warnings,
    )


__all__ = ["PrintTimeEstimate", "estimate_print_time", "EstimationError"]
