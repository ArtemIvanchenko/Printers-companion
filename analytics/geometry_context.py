"""Map process anomalies back to build height and STL geometry.

The process analytics and geometry estimator intentionally run independently:
logs can be analysed without an STL and a plate can be estimated before logs
exist.  Once a print card contains both, this module joins their additive JSON
snapshots without re-slicing a potentially very large plate.

Layer-duration anomalies carry an exact printer layer.  Sensor anomalies carry
their telemetry sample index; these are mapped to a layer by relative progress
through the active print and are explicitly marked ``approximate``.  Consumers
must not present the latter as an exact layer measurement.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from analytics.prediction.layer_engine import GEOMETRY_FEATURES, LayerGeometrySeries


_FEATURE_NAMES_RU = {
    "hatch_mm": "штриховка объёма",
    "contour_mm": "обводка контуров",
    "jump_mm": "перемещения лазера без плавления",
    "open_mm": "тонкостенные поддержки",
}


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _logged_layers(telemetry: dict[str, Any]) -> list[int]:
    return sorted({
        int(point["layer"])
        for point in (telemetry.get("layer_burn_times") or [])
        if isinstance(point, dict)
        and isinstance(point.get("layer"), int)
    })


def _layer_from_sample(anomaly: dict[str, Any], telemetry: dict[str, Any]) -> int | None:
    sample = anomaly.get("sample_index")
    layers = _logged_layers(telemetry)
    if not isinstance(sample, int) or not layers:
        return None
    time_axis = telemetry.get("time") or []
    point_count = len(time_axis)
    if point_count < 2:
        # Some synthetic or legacy payloads omit the time labels while signal
        # arrays remain aligned. Infer their common length as a safe fallback.
        point_count = max(
            (len(values) for group in telemetry.values() if isinstance(group, dict)
             for values in group.values() if isinstance(values, list)),
            default=0,
        )
    if point_count < 2:
        return None
    progress = min(max(sample / (point_count - 1), 0.0), 1.0)
    return layers[round(progress * (len(layers) - 1))]


def _layer_ordinal(layer: int, layer_count: int, logged_layers: list[int]) -> int:
    """Convert printer layer numbering to a zero-based geometry ordinal."""
    if 1 <= layer <= layer_count:
        return layer - 1
    if 0 <= layer < layer_count:
        return layer
    # Restarted/partial logs may use a continued counter outside this plate's
    # nominal range. Preserve relative order, but label it approximate upstream.
    if layer in logged_layers and len(logged_layers) > 1:
        rank = logged_layers.index(layer)
        return round(rank / (len(logged_layers) - 1) * (layer_count - 1))
    return min(max(layer - 1, 0), max(layer_count - 1, 0))


def _geometry_load(series: LayerGeometrySeries, z_mm: float) -> tuple[dict[str, float], str, float | None]:
    values = dict(zip(GEOMETRY_FEATURES, series.at(z_mm)))
    comparable = {name: values[name] for name in _FEATURE_NAMES_RU}
    local_load = sum(comparable.values())
    if local_load <= 0:
        dominant = "на этом слое нет сканируемой траектории"
    else:
        dominant = _FEATURE_NAMES_RU[max(comparable, key=comparable.get)]
    # ``series.zs`` also includes extra body-boundary samples and is therefore
    # non-uniform. A median over it overweights geometry transitions. Evaluate
    # a small uniform Z grid so 100% really means a typical physical layer.
    grid_size = min(max(int(series.height_mm / 0.5) + 1, 25), 101)
    sample_loads = [
        sum(dict(zip(GEOMETRY_FEATURES, series.at(z)))[name] for name in _FEATURE_NAMES_RU)
        for z in (
            [series.z_min]
            if grid_size <= 1 else
            [
                series.z_min + index * series.height_mm / (grid_size - 1)
                for index in range(grid_size)
            ]
        )
    ]
    typical = statistics.median(sample_loads) if sample_loads else 0.0
    relative = local_load / typical * 100.0 if typical > 0 else None
    rounded = {name: round(float(value), 1) for name, value in values.items()}
    return rounded, dominant, relative


def _active_bodies(
    z_mm: float,
    regions: list[dict[str, Any]],
    z_range_mm: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    range_low, range_high = z_range_mm or (z_mm, z_mm)
    for region in regions:
        low = _finite_number(region.get("z_min_mm"))
        high = _finite_number(region.get("z_max_mm"))
        if low is None or high is None or high < range_low or low > range_high:
            continue
        has_activity_contract = "active_z_intervals_mm" in region
        raw_intervals = region.get("active_z_intervals_mm")
        intervals = [
            (interval_low, interval_high)
            for interval in (raw_intervals if isinstance(raw_intervals, list) else [])
            if isinstance(interval, (list, tuple)) and len(interval) == 2
            and (interval_low := _finite_number(interval[0])) is not None
            and (interval_high := _finite_number(interval[1])) is not None
            and interval_low <= interval_high
        ]
        if has_activity_contract:
            if not intervals or not any(
                end >= range_low and start <= range_high for start, end in intervals
            ):
                continue
        active.append({
            "name": str(region.get("name") or "STL"),
            "kind": region.get("kind") or "part",
            "kind_ru": "поддержка" if region.get("kind") == "support" else "деталь",
            # A chamber sensor has no XY position. This is the body's possible
            # footprint at the mapped height, not a claimed defect coordinate.
            "xy_bounds_mm": region.get("xy_bounds_mm"),
            "presence_evidence": "sampled_section" if has_activity_contract else "height_bounds_only",
            "presence_note_ru": (
                "STL имеет сечение в ближайшем рассчитанном диапазоне высот"
                if has_activity_contract else
                "Старый снимок: известно только пересечение габарита тела по высоте"
            ),
        })
    return active


def map_anomalies_to_geometry(
    health: dict[str, Any] | None,
    scan_geometry: dict[str, Any] | None,
    *,
    geometry_regions: list[dict[str, Any]] | None = None,
    telemetry: dict[str, Any] | None = None,
    geometry_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return operator-facing locations for process and layer anomalies.

    The result is additive and JSON serialisable. ``status=unavailable`` is a
    normal outcome when either logs or a geometry snapshot are absent.
    """
    health = health or {}
    telemetry = telemetry or {}
    geometry_regions = geometry_regions or []
    geometry_quality = geometry_quality or {}
    if not isinstance(scan_geometry, dict):
        return {"status": "unavailable", "reason_ru": "Нет послойного снимка геометрии STL", "items": []}
    try:
        series = LayerGeometrySeries.from_snapshot(scan_geometry)
        thickness = float(scan_geometry["layer_thickness_mm"])
    except (KeyError, TypeError, ValueError):
        return {"status": "unavailable", "reason_ru": "Снимок геометрии неполон", "items": []}
    if thickness <= 0 or not series.zs:
        return {"status": "unavailable", "reason_ru": "Некорректная толщина слоя или пустая геометрия", "items": []}

    candidates: list[tuple[dict[str, Any], str]] = []
    for outlier in ((health.get("burn_drift") or {}).get("outlier_layers") or []):
        if isinstance(outlier, dict):
            candidates.append((outlier, "layer_duration"))
    for anomaly in health.get("anomalies") or []:
        if not isinstance(anomaly, dict):
            continue
        ranges = anomaly.get("sample_ranges") if anomaly.get("kind") == "threshold" else None
        if isinstance(ranges, list) and ranges:
            for sample_range in ranges[:20]:
                if isinstance(sample_range, dict):
                    candidates.append(({**anomaly, "_sample_range": sample_range}, "process_signal"))
        else:
            candidates.append((anomaly, "process_signal"))

    layer_count = int(scan_geometry.get("layer_count") or series.layer_count(thickness))
    logged_layers = _logged_layers(telemetry)
    items: list[dict[str, Any]] = []
    for anomaly, anomaly_type in candidates:
        explicit_layer = anomaly.get("layer")
        source_precision = anomaly.get("layer_mapping_precision")
        exact = isinstance(explicit_layer, int) and not source_precision
        mapped_range = anomaly.get("layer_range")
        sample_range = anomaly.get("_sample_range") or {}
        if sample_range:
            start = sample_range.get("start") or {}
            end = sample_range.get("end") or {}

            def endpoint_layer(endpoint: dict[str, Any], *, is_start: bool) -> int | None:
                if isinstance(endpoint.get("layer"), int):
                    return int(endpoint["layer"])
                bracket = endpoint.get("layer_range")
                if (
                    isinstance(bracket, (list, tuple))
                    and len(bracket) == 2
                    and all(isinstance(value, int) for value in bracket)
                ):
                    # The event begins after the lower timestamp bracket and
                    # ends before the upper one: use the inner boundaries so
                    # an alarm at 40..41 through 60..61 maps to 41..60.
                    return max(bracket) if is_start else min(bracket)
                # A timestamp with no burn-log bracket must not silently turn
                # into a whole-build progress estimate. Progress is the
                # explicit fallback only for telemetry without timestamps.
                if endpoint.get("timestamp"):
                    return None
                return _layer_from_sample(endpoint, telemetry)

            start_layer = endpoint_layer(start, is_start=True)
            end_layer = endpoint_layer(end, is_start=False)
            if isinstance(start_layer, int) and isinstance(end_layer, int):
                mapped_range = [min(start_layer, end_layer), max(start_layer, end_layer)]
        if (
            isinstance(mapped_range, (list, tuple)) and len(mapped_range) == 2
            and all(isinstance(value, int) for value in mapped_range)
        ):
            layer_range = [min(mapped_range), max(mapped_range)]
            layer = round(sum(layer_range) / 2)
            exact = False
            precision = (
                "timestamp_range"
                if any((sample_range.get(side) or {}).get("timestamp") for side in ("start", "end"))
                else "approximate_progress_range"
            )
        else:
            layer_range = None
            layer = explicit_layer if isinstance(explicit_layer, int) else _layer_from_sample(anomaly, telemetry)
            precision = (
                "exact_layer" if exact
                else "nearest_logged_layer" if source_precision == "nearest_burn_log_timestamp"
                else "approximate_progress"
            )
        if not isinstance(layer, int):
            continue
        ordinal = _layer_ordinal(layer, layer_count, logged_layers)
        if exact and not (0 <= layer <= layer_count):
            exact = False
        z_mm = min(series.z_max, max(series.z_min, series.z_min + (ordinal + 0.5) * thickness))
        components, dominant, relative_load = _geometry_load(series, z_mm)
        if layer_range:
            low_ordinal = _layer_ordinal(layer_range[0], layer_count, logged_layers)
            high_ordinal = _layer_ordinal(layer_range[1], layer_count, logged_layers)
            z_low = min(series.z_max, max(series.z_min, series.z_min + low_ordinal * thickness))
            z_high = min(
                series.z_max,
                max(series.z_min, series.z_min + (high_ordinal + 1) * thickness),
            )
        else:
            z_low = max(series.z_min, z_mm - thickness / 2)
            z_high = min(series.z_max, z_mm + thickness / 2)
        active = _active_bodies(z_mm, geometry_regions, (z_low, z_high))
        height = max(0.0, z_mm - series.z_min)
        names = ", ".join(body["name"] for body in active[:4])
        layer_label = (
            f"диапазону слоёв {layer_range[0]}–{layer_range[1]}"
            if layer_range else f"слою {layer}"
        )
        conclusion = (
            f"Отклонение соответствует {layer_label}, высоте около {height:.2f} мм; "
            f"наибольшая длина траектории — {dominant}."
        )
        if names:
            conclusion += f" На ближайшем рассчётном сечении присутствуют: {names}."
        items.append({
            "anomaly_type": anomaly_type,
            "signal": anomaly.get("signal"),
            "kind": anomaly.get("kind") or ("burn_time_outlier" if anomaly_type == "layer_duration" else None),
            "layer": layer,
            "layer_range": layer_range,
            "height_mm": round(height, 3),
            "z_mm": round(z_mm, 3),
            "height_range_mm": [
                round(max(0.0, z_low - series.z_min), 3),
                round(min(series.height_mm, z_high - series.z_min), 3),
            ],
            "mapping_precision": precision,
            "layer_precision": precision,
            "mapping_note_ru": (
                "Точный номер слоя взят из журнала времени"
                if exact else
                "Диапазон привязан по времени начала/конца отклонения"
                if layer_range else
                "Слой сопоставлен с ближайшей меткой burn.log"
                if precision == "nearest_logged_layer" else
                "Слой грубо оценён по доле пройденной печати"
            ),
            "geometry": {
                "dominant_operation_ru": dominant,
                "largest_path_component_ru": dominant,
                "relative_load_pct": round(relative_load, 1) if relative_load is not None else None,
                "relative_path_length_pct": (
                    round(relative_load, 1) if relative_load is not None else None
                ),
                **components,
            },
            "active_bodies": active,
            "conclusion_ru": conclusion,
            "severity": anomaly.get("severity"),
            "detail": anomaly.get("detail"),
        })

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, None: 4}
    items.sort(key=lambda item: (
        0 if item["mapping_precision"] == "exact_layer" else 1,
        severity_order.get(item.get("severity"), 4),
        item.get("layer") or 0,
    ))

    has_sampled_activity = any(
        "active_z_intervals_mm" in region for region in geometry_regions
        if isinstance(region, dict)
    )
    quality_status = geometry_quality.get("status") or "standard"
    origin_source = geometry_quality.get("build_origin_source")
    origin_confirmed = origin_source in {"explicit", "confirmed_magics_plate_datum"}
    origin_unconfirmed = not origin_confirmed
    geometry_level = (
        "low" if quality_status in {"incomplete", "lower_bound"} or origin_unconfirmed
        else "medium" if has_sampled_activity else "low"
    )
    return {
        "status": "ok" if items else "no_mappable_anomalies",
        "build_axis": "Z",
        "layer_thickness_mm": thickness,
        "items": items,
        "exact_count": sum(item["mapping_precision"] == "exact_layer" for item in items),
        "approximate_count": sum(item["mapping_precision"] != "exact_layer" for item in items),
        "geometry_confidence": {
            "level": geometry_level,
            "source": "sampled_sections" if has_sampled_activity else "body_height_bounds",
            "geometry_quality_status": quality_status,
            "build_origin_source": origin_source,
            "build_origin_confirmed": origin_confirmed,
            "note_ru": (
                "Начало печати по Z не подтверждено: высота и номер физического слоя могут быть смещены."
                if origin_unconfirmed else
                "Привязка использует выбранные сечения STL; между ними геометрия интерполируется."
                if has_sampled_activity else
                "Старый снимок не хранит активность тел по сечениям; использованы только габариты по Z."
            ),
        },
        "limitation_ru": (
            "Датчиковые события без номера слоя привязываются приблизительно по ходу печати; "
            "послойные отклонения времени имеют точный номер слоя, но геометрия между расчётными сечениями интерполируется. "
            "Координату X/Y по общему датчику определить нельзя — показаны только возможные STL-тела и их границы."
        ),
    }


__all__ = ["map_anomalies_to_geometry"]
