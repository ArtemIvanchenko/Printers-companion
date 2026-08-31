"""Physics-informed virtual sensors derived from existing M-350 channels."""

from __future__ import annotations

import math
from statistics import median
from typing import Any

from analytics.robust_stats import theil_sen_slope


def _finite(values: list[Any]) -> list[float]:
    return [
        float(value)
        for value in values
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ]


def _aligned(*series: list[Any]) -> list[tuple[float, ...]]:
    rows: list[tuple[float, ...]] = []
    for values in zip(*series):
        if all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in values
        ):
            rows.append(tuple(float(value) for value in values))
    return rows


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def _pick(group: dict[str, list], names: tuple[str, ...]) -> tuple[str, list] | None:
    for name in names:
        values = group.get(name)
        if isinstance(values, list) and values:
            return name, values
    return None


def compute_soft_sensors(
    telemetry: dict[str, Any],
    signal_stats: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate transparent virtual measurements, never opaque health truth.

    Every result names its raw inputs, formula and confidence. Missing or
    physically invalid channels simply omit that metric.
    """
    metrics: list[dict[str, Any]] = []
    stats = signal_stats or {}

    oxygen = telemetry.get("oxygen") or {}
    if "SO1" in oxygen and "SO2" in oxygen:
        rows = _aligned(oxygen["SO1"], oxygen["SO2"])
        if len(rows) >= 5:
            differences = [abs(first - second) for first, second in rows]
            p95 = _percentile(differences, 0.95)
            metrics.append({
                "key": "oxygen_sensor_disagreement",
                "name_ru": "Расхождение двух датчиков кислорода",
                "value": round(median(differences), 4),
                "p95": round(p95, 4),
                "unit": "% O₂",
                "status": "warning" if p95 > 0.5 else "ok",
                "confidence": 0.85,
                "inputs": ["SO1", "SO2"],
                "method": "медиана и 95-й процентиль |SO1 − SO2|",
            })

    temperatures = telemetry.get("temperatures") or {}
    active_temperature_series = [
        values for name, values in temperatures.items()
        if name in {"ST3", "ST4", "ST5"} and len(_finite(values)) >= 5
    ]
    if len(active_temperature_series) >= 2:
        rows = _aligned(*active_temperature_series)
        gradients = [max(row) - min(row) for row in rows if max(row) > 1.0]
        if gradients:
            metrics.append({
                "key": "thermal_nonuniformity",
                "name_ru": "Неравномерность температуры по зонам камеры",
                "value": round(median(gradients), 3),
                "p95": round(_percentile(gradients, 0.95), 3),
                "unit": "°C",
                "status": "diagnostic",
                "confidence": 0.7,
                "inputs": [name for name in ("ST3", "ST4", "ST5") if name in temperatures],
                "method": "размах температур активных зон в один момент времени",
            })

    humidity_pick = _pick(
        telemetry.get("humidity") or {},
        ("Flow H", "ST1 (flow H)"),
    )
    temperature_pick = _pick(
        telemetry.get("gas_temperature") or {},
        ("Flow T", "ST1 (flow T)"),
    )
    if humidity_pick and temperature_pick:
        humidity_name, humidity_values = humidity_pick
        temperature_name, temperature_values = temperature_pick
        rows = _aligned(temperature_values, humidity_values)
        dew_points: list[float] = []
        for temperature, relative_humidity in rows:
            if not (-10 <= temperature <= 80 and 0 < relative_humidity <= 100):
                continue
            # Magnus approximation over the machine's confirmed gas range.
            gamma = math.log(relative_humidity / 100.0) + 17.62 * temperature / (243.12 + temperature)
            dew_points.append(243.12 * gamma / (17.62 - gamma))
        if dew_points:
            metrics.append({
                "key": "purge_gas_dew_point",
                "name_ru": "Расчётная точка росы продувочного газа",
                "value": round(median(dew_points), 2),
                "p95": round(_percentile(dew_points, 0.95), 2),
                "unit": "°C",
                "status": "experimental",
                "confidence": 0.65,
                "inputs": [temperature_name, humidity_name],
                "method": "формула Магнуса по температуре и относительной влажности",
            })

    chamber_pressure = _finite((telemetry.get("pressure") or {}).get("SP4") or [])
    if len(chamber_pressure) >= 5:
        slope = theil_sen_slope(chamber_pressure)
        relative_change = (
            slope * (len(chamber_pressure) - 1) / abs(median(chamber_pressure)) * 100
            if median(chamber_pressure) else 0.0
        )
        metrics.append({
            "key": "chamber_pressure_drift",
            "name_ru": "Дрейф давления рабочей камеры",
            "value": round(relative_change, 3),
            "unit": "% за наблюдаемый интервал",
            "status": "warning" if abs(relative_change) > 5 else "ok",
            "confidence": 0.75,
            "inputs": ["SP4"],
            "method": "робастный наклон Тейла—Сена, приведённый к длине интервала",
        })

    before = (stats.get("SP11") or {}).get("mean")
    after = (stats.get("SP12") or {}).get("mean")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        metrics.append({
            "key": "filter_pressure_drop_proxy",
            "name_ru": "Расчётный перепад давления на контуре фильтров",
            "value": round(float(before) - float(after), 5),
            "unit": "ед. давления каналов",
            "status": "experimental",
            "confidence": 0.45,
            "inputs": ["SP11", "SP12"],
            "method": "среднее SP11 − среднее SP12; назначение каналов пока кандидатное",
        })

    return {
        "metrics": metrics,
        "available": len(metrics),
        "method_version": "soft-sensors-0.1.0",
    }


__all__ = ["compute_soft_sensors"]
