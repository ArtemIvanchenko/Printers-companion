"""Non-overlapping layer-cycle accounting and post-resume timing comparisons."""
from statistics import median

from analytics.log_insights.clocks import iso
from analytics.prediction.timing_evidence import summarize_timing_events
from analytics.prediction.timing_validation import (
    MAX_BURN_MS, MAX_POUR_MS, MIN_BURN_MS, MIN_POUR_MS,
    calibration_timing_payloads, finite_number, normal_phase_exclusions,
)


def time_accounting(events, pauses=(), cycle_model=None):
    evidence = summarize_timing_events(events)
    model = cycle_model or {}
    base, floor = model.get("layer_overhead_ms"), model.get("minimum_layer_cycle_ms")
    calibrated = finite_number(base) and finite_number(floor) and base >= 0 and floor >= 0
    totals = {key: 0.0 for key in ("laser_scan", "powder_recoat", "base_overhead",
                                  "minimum_cycle_wait", "unexplained_residual")}
    normal_seconds = 0.0
    normal_layers = set()
    phase_exclusions = []
    rows = []
    for row in evidence.attempts:
        phase_reasons = normal_phase_exclusions({"burn_ms": row.burn_ms, "pour_ms": row.pour_ms})
        if phase_reasons:
            phase_exclusions.append({
                "layer": row.layer, "reasons": phase_reasons,
                "burn_seconds": row.burn_ms / 1000, "pour_seconds": row.pour_ms / 1000,
            })
        totals["laser_scan"] += row.burn_ms / 1000
        totals["powder_recoat"] += row.pour_ms / 1000
        residual = max(row.overhead_ms, 0.0)
        base_part = min(residual, base) if calibrated else 0.0
        floor_part = min(residual - base_part, max(floor - row.burn_ms - row.pour_ms - base, 0)) if calibrated else 0.0
        unexplained = residual - base_part - floor_part
        totals["base_overhead"] += base_part / 1000
        totals["minimum_cycle_wait"] += floor_part / 1000
        totals["unexplained_residual"] += unexplained / 1000
        if unexplained > 0:
            rows.append({"layer": row.layer, "unexplained_seconds": unexplained / 1000})
        if not phase_reasons and row.layer not in evidence.ambiguous_layers:
            normal_layers.add(row.layer)
            if calibrated:
                normal_seconds += max(row.burn_ms + row.pour_ms + base, floor) / 1000
    names = {"laser_scan": "Прожиг всех записанных попыток", "powder_recoat": "Нанесение порошка всех попыток",
             "base_overhead": "Штатная межфазная задержка", "minimum_cycle_wait": "Ожидание минимального цикла",
             "unexplained_residual": "Необъяснённый остаток цикла"}
    first_seconds = sum(row.make_layer_ms for row in evidence.cycles.values()) / 1000
    all_seconds = sum(row.make_layer_ms for row in evidence.attempts) / 1000
    rows.sort(key=lambda row: row["unexplained_seconds"], reverse=True)
    return {
        "status": "ok" if evidence.attempts else "insufficient_data",
        "source": "calibrated" if calibrated else "calculated",
        "sample_count": len(evidence.attempts),
        "components": [{"key": key, "name_ru": names[key], "seconds": value} for key, value in totals.items()],
        "observed_attempt_cycle_seconds": all_seconds,
        "normal_unique_layer_seconds": normal_seconds if calibrated and normal_layers else None,
        "normal_layer_count": len(normal_layers),
        "normal_time_scope": "eligible_measured_layers_only",
        "phase_excluded_layer_count": len({row["layer"] for row in phase_exclusions}),
        "phase_exclusions": phase_exclusions[:100],
        "normal_phase_admission": {
            "source": "heuristic", "confirmed": False,
            "burn_ms_range": [MIN_BURN_MS, MAX_BURN_MS],
            "pour_ms_range": [MIN_POUR_MS, MAX_POUR_MS],
        },
        "repeat_attempt_seconds": max(0.0, all_seconds - first_seconds),
        "repeat_attempt_count": evidence.repeated_attempt_rows,
        "ambiguous_layer_count": len(evidence.ambiguous_layers),
        "explicit_pause_seconds": sum(p["end"] - p["start"] for p in pauses if p["end"] is not None),
        "open_pause_count": sum(p["end"] is None for p in pauses),
        "largest_residuals": rows[:100],
        "limitations_ru": [
            "Компоненты складываются в записанное время всех попыток; время пауз и повторов показано отдельно и не прибавляется повторно.",
            "Необъяснённый остаток не объявляется паузой без события в журнале.",
            "Без калибровки минимального цикла нормальное полное время не определяется.",
            "Нормальная сумма относится только к допустимым измеренным слоям, не ко всей печати при пропусках.",
            "Выход длительности фазы за предварительный диапазон исключает слой из нормальной суммы, но не доказывает паузу или повреждение лога.",
            "Разные попытки установлены по различию записей; одинаковые длительности сами по себе не доказывают отсутствие повтора.",
        ],
    }


def restart_layer_comparison(events, windows, pauses, count=5):
    timings = calibration_timing_payloads(events)
    result = []
    for pause in pauses:
        if not pause["resumed"]:
            continue
        before = [w["layer"] for w in windows if w["end"] <= pause["start"]][-count:]
        after = [w["layer"] for w in windows if w["start"] >= pause["end"]][:count]
        metrics = {}
        for key in ("burn_ms", "pour_ms"):
            left = [timings[layer][key] for layer in before if finite_number(timings.get(layer, {}).get(key))]
            right = [timings[layer][key] for layer in after if finite_number(timings.get(layer, {}).get(key))]
            if len(left) >= 2 and len(right) >= 2:
                baseline = median(left)
                metrics[key] = {"before_median_seconds": baseline / 1000,
                                "after_median_seconds": median(right) / 1000,
                                "before_sample_count": len(left), "after_sample_count": len(right),
                                "change_pct": (median(right) / baseline - 1) * 100 if baseline else None}
        result.append({"resume_timestamp": iso(pause["end"]), "before_layers": before,
                       "after_layers": after, "metrics": metrics})
    return {"source": "calculated", "status": "ok" if result else "no_resumes",
            "sample_count": len(result), "items": result[:100],
            "limitations_ru": ["Изменение времени после возобновления может объясняться геометрией; причинность не установлена."]}
