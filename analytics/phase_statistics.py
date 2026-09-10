"""Exact per-layer phase statistics from the printer's own time log."""

from __future__ import annotations

import math
from statistics import median
from typing import Any

from analytics.robust_stats import theil_sen_slope
from analytics.prediction.timing_validation import calibration_timing_payloads


def _value(item: Any, name: str, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _summary(values_by_layer: dict[int, float], name_ru: str) -> dict[str, Any]:
    layers = sorted(values_by_layer)
    values = [values_by_layer[layer] for layer in layers]
    ordered = sorted(values)
    med = median(values)
    mad = median(abs(value - med) for value in values)
    outliers = []
    if mad > 0:
        outliers = [
            layer for layer, value in zip(layers, values)
            if abs(0.6745 * (value - med) / mad) >= 3.5
        ]
    mean = sum(values) / len(values)
    std = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    return {
        "name_ru": name_ru,
        "layers": len(values),
        "total_sec": round(sum(values), 3),
        "mean_sec_per_layer": round(mean, 4),
        "median_sec_per_layer": round(med, 4),
        "p95_sec_per_layer": round(ordered[round((len(ordered) - 1) * 0.95)], 4),
        "coefficient_of_variation": round(std / abs(mean), 4) if mean else None,
        "theil_sen_sec_per_layer": round(theil_sen_slope(values), 6) if len(values) >= 3 else None,
        "outlier_layers": outliers[:50],
    }


def compute_layer_phase_statistics(events: list[Any]) -> dict[str, Any]:
    """Split layer cycle into scan, recoat and residual controller overhead."""
    timing: dict[int, dict[str, float]] = {}
    summaries = calibration_timing_payloads(events)
    has_summaries = any(_value(e, "event_type") == "layer_timing_summary" for e in events)
    absolute: dict[int, dict[str, float]] = {}
    for event in events:
        payload = _value(event, "payload", {}) or {}
        layer = payload.get("layer", _value(event, "layer"))
        if not isinstance(layer, int):
            continue
        event_type = str(_value(event, "event_type", ""))
        if event_type == "layer_timing_summary":
            if layer not in summaries:
                continue
            payload = summaries[layer]
            if layer in timing:
                continue  # deterministic first-wins for repeated firmware dumps
            timing[layer] = {
                "scan": float(payload.get("burn_ms") or 0) / 1000,
                "recoat": float(payload.get("pour_ms") or 0) / 1000,
                "cycle": float(payload.get("make_layer_ms") or 0) / 1000,
            }
        elif isinstance(payload.get("abs_ms"), int):
            absolute.setdefault(layer, {})[event_type] = float(payload["abs_ms"])

    if not timing and not has_summaries:
        for layer, values in absolute.items():
            burn_start, burn_end = values.get("burn_start"), values.get("burn_end")
            pour_start, pour_end = values.get("pour_start"), values.get("pour_end")
            scan = (burn_end - burn_start) / 1000 if burn_start is not None and burn_end is not None else 0
            recoat = (pour_end - pour_start) / 1000 if pour_start is not None and pour_end is not None else 0
            if scan > 0 or recoat > 0:
                timing[layer] = {"scan": max(0.0, scan), "recoat": max(0.0, recoat), "cycle": max(0.0, scan + recoat)}

    valid = {
        layer: values for layer, values in timing.items()
        if values["scan"] > 0 and values["recoat"] >= 0 and values["cycle"] > 0
    }
    if not valid:
        return {"available": False, "layer_count": 0, "phases": {}}

    scan = {layer: values["scan"] for layer, values in valid.items()}
    recoat = {layer: values["recoat"] for layer, values in valid.items()}
    cycle = {layer: values["cycle"] for layer, values in valid.items()}
    overhead = {
        layer: max(0.0, values["cycle"] - values["scan"] - values["recoat"])
        for layer, values in valid.items()
    }
    phases = {
        "laser_scan": _summary(scan, "Лазерное сканирование"),
        "powder_recoat": _summary(recoat, "Нанесение и разравнивание порошка"),
        "controller_overhead": _summary(overhead, "Остаток цикла: ожидания и возможные остановки"),
        "full_layer_cycle": _summary(cycle, "Полный машинный цикл слоя"),
    }
    total = phases["full_layer_cycle"]["total_sec"] or 1.0
    phases["laser_scan"]["share_pct"] = round(phases["laser_scan"]["total_sec"] / total * 100, 2)
    phases["powder_recoat"]["share_pct"] = round(phases["powder_recoat"]["total_sec"] / total * 100, 2)
    phases["controller_overhead"]["share_pct"] = round(phases["controller_overhead"]["total_sec"] / total * 100, 2)
    return {
        "available": True,
        "layer_count": len(valid),
        "first_layer": min(valid),
        "last_layer": max(valid),
        "phases": phases,
        "method_version": "layer-phases-0.2.0",
        "source": "time_log layer_timing_summary / NEW_STATS",
    }


__all__ = ["compute_layer_phase_statistics"]
