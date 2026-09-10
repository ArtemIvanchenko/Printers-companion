"""Geometry-adjusted scan residuals, true-repeat comparisons and inspection regions."""
from statistics import median

from analytics.geometry_context import _active_bodies
from analytics.prediction.layer_engine import GEOMETRY_FEATURES, LayerGeometrySeries
from analytics.prediction.timing_validation import finite_number


def geometry_residuals(timings, snapshot, session_id=None, record_id=None):
    reference = snapshot.get("scan_timing_reference") or {}
    unavailable = {"status": "insufficient_data", "source": "calculated", "sample_count": 0, "items": []}
    if snapshot.get("build_origin_source") not in {"explicit", "confirmed_magics_plate_datum"}:
        return {**unavailable, "reason_ru": "Не подтверждено начало построения по Z."}
    if not snapshot.get("scan_geometry") or not reference:
        return {**unavailable, "reason_ru": "Нужен новый снимок прогноза с геометрией и параметрами прожига."}
    if (snapshot.get("geometry_quality") or {}).get("status") in {"incomplete", "lower_bound"}:
        return {**unavailable, "reason_ru": "Геометрия неполна: остаток времени может объясняться отсутствующими поддержками."}
    beta = reference.get("beta") or []
    thickness, origin, lasers = (snapshot.get(key) for key in ("layer_thickness_mm", "build_origin_z_mm", "laser_count"))
    if (len(beta) != len(GEOMETRY_FEATURES) + 1 or not all(finite_number(v) and v >= 0 for v in beta)
            or not all(finite_number(v) for v in (thickness, origin, lasers)) or thickness <= 0 or lasers < 1):
        return {**unavailable, "reason_ru": "Некорректные параметры снимка прогноза."}
    try:
        series = LayerGeometrySeries.from_snapshot(snapshot["scan_geometry"])
        if (not series.zs or any(not finite_number(z) for z in series.zs)
                or any(b <= a for a, b in zip(series.zs, series.zs[1:]))
                or any(len(getattr(series, key)) != len(series.zs)
                       or not all(finite_number(v) and v >= 0 for v in getattr(series, key)) for key in GEOMETRY_FEATURES)):
            raise ValueError("invalid sampled geometry")
    except (KeyError, ValueError, TypeError):
        return {**unavailable, "reason_ru": "Повреждён снимок геометрии."}
    rows = []
    for layer, values in sorted(timings.items()):
        z = origin + (layer - 0.5) * thickness
        burn = values.get("burn_ms")
        if not finite_number(burn) or not series.z_min <= z <= series.z_max:
            continue
        expected = sum(b*g for b, g in zip(beta, series.at(z))) / lasers + beta[-1]
        if expected <= 0:
            continue
        observed = burn / 1000
        rows.append({"layer": layer, "height_mm": z, "observed_seconds": observed,
                     "expected_seconds": expected, "residual_seconds": observed - expected,
                     "relative_error_pct": (observed / expected - 1) * 100})
    errors = [row["relative_error_pct"] for row in rows]
    centre = median(errors) if errors else 0
    mad = median(abs(e - centre) for e in errors) if errors else 0
    for row in rows:
        row["atypical"] = len(rows) >= 10 and abs(row["relative_error_pct"] - centre) > max(20, 3.5 * 1.4826 * mad)
    rows.sort(key=lambda row: abs(row["relative_error_pct"] - centre), reverse=True)
    in_sample = (session_id in reference.get("training_session_ids", [])
                 or record_id in reference.get("source_records", []))
    return {"status": "ok" if rows else "insufficient_data", "source": reference.get("source", "heuristic"),
            "sample_count": len(rows), "in_sample": in_sample, "model_version": reference.get("model_version"),
            "median_bias_pct": centre if rows else None,
            "atypical_layer_count": sum(row["atypical"] for row in rows), "items": rows[:100],
            "limitations_ru": ["Сечения интерполированы; остаток модели не доказывает неисправность или дефект.",
                               "Оценка на обучающих печатях не является проверкой точности модели." if in_sample
                               else "Физический расчёт без подтверждённых стратегий сканирования остаётся приближённым."]}


def scan_reference(params, material, thickness, correction_factor=1.0):
    """Freeze the actual model/physical coefficients used by the estimator."""
    from analytics.prediction.layer_engine import resolve_scan_model
    from analytics.prediction.plate_estimator import _DEFAULT_JUMP_SPEED_MM_S
    from core.versioning.provenance import stable_hash

    model = resolve_scan_model(params, material, thickness)
    if model:
        return {"beta": list(model["beta"]), "source": "calibrated", "model_version": stable_hash(model),
                "source_records": model.get("source_records", []),
                "training_session_ids": model.get("training_session_ids", [])}
    hatch = float((params.get("hatch_speeds_by_mat") or {}).get(material) or params.get("hatch_speed_mm_s") or 0)
    if hatch <= 0:
        return {}
    beta = [1/hatch, 1/float(params.get("contour_speed_mm_s") or hatch),
            1/float(params.get("jump_speed_mm_s") or _DEFAULT_JUMP_SPEED_MM_S),
            float(params.get("jump_delay_ms") or 0)/1000,
            1/float(params.get("support_speed_mm_s") or hatch), 0]
    return {"beta": [b*correction_factor for b in beta], "source": "heuristic",
            "model_version": "preset-path-time-v1", "source_records": []}


def compare_repeats(target, references, limit=10):
    """Compare only identical, confirmed layout + complete machine recipe."""
    rows = []
    key = target.get("comparison_key")
    if not key:
        return {"status": "insufficient_identity", "source": "calculated", "sample_count": 0, "items": [],
                "reason_ru": "Нужны fingerprint компоновки, подтверждённая связь с логом и полный режим печати."}
    for ref in references:
        if ref.get("comparison_key") != key or ref.get("session_id") == target.get("session_id"):
            continue
        left, right = ref.get("timings", {}), target.get("timings", {})
        common = sorted(left.keys() & right.keys())
        differences = []
        metrics = {}
        for name in ("burn_ms", "pour_ms", "make_layer_ms"):
            points = []
            for layer in common:
                a, b = left[layer].get(name), right[layer].get(name)
                if finite_number(a) and finite_number(b) and a > 0:
                    points.append((layer, (b/a - 1)*100, (b-a)/1000))
            if points:
                metrics[name] = {"median_change_pct": median(p[1] for p in points),
                                 "total_difference_seconds": sum(p[2] for p in points), "layers": len(points)}
                differences.extend({"layer": p[0], "metric": name, "change_pct": p[1], "difference_seconds": p[2]} for p in points)
        differences.sort(key=lambda row: abs(row["difference_seconds"]), reverse=True)
        ref_env = {m["signal"]: m for m in ref.get("environment", [])}
        env_changes = []
        for candidate in target.get("environment", []):
            previous = ref_env.get(candidate["signal"])
            if (not previous or previous.get("threshold") != candidate.get("threshold")
                    or min(previous.get("coverage_ratio") or 0, candidate.get("coverage_ratio") or 0) < 0.8):
                continue
            a, b = previous.get("time_weighted_mean"), candidate.get("time_weighted_mean")
            if finite_number(a) and finite_number(b):
                env_changes.append({"signal": candidate["signal"], "name_ru": candidate.get("name_ru"),
                                    "mean_difference": b-a,
                                    "unit": (candidate.get("threshold") or {}).get("unit"),
                                    "scope": "covered_burn_intervals"})
        rows.append({"reference_session_id": ref["session_id"], "common_layers": len(common),
                     "coverage_ratio": len(common) / max(len(left), len(right), 1),
                     "metrics": metrics, "largest_differences": differences[:30],
                     "environment_changes": env_changes,
                     "environment": {
                         "reference": ref.get("environment", []), "candidate": target.get("environment", []),
                     }})
    return {"status": "ok" if rows else "no_confirmed_repeats", "source": "calculated",
            "sample_count": len(rows), "items": rows[:limit],
            "limitations_ru": ["Сопоставляются одинаковые физические номера слоёв; пропуски не растягиваются.",
                               "Полный фактический цикл может содержать паузы. Различие само по себе не устанавливает причину."]}


def inspection_map(environment, residuals, snapshot):
    signals = {}
    for item in environment.get("layer_items", []):
        signals.setdefault(item["layer"], []).extend(
            {"kind": "environment", "signal": key, "exceedance_seconds": row["exceedance_seconds"]}
            for key, row in item["signals"].items() if row["exceedance_seconds"] > 0
        )
    for item in residuals.get("items", []):
        if item.get("atypical"):
            signals.setdefault(item["layer"], []).append({"kind": "scan_residual", "residual_seconds": item["residual_seconds"]})
    thickness, origin = snapshot.get("layer_thickness_mm"), snapshot.get("build_origin_z_mm")
    known = (snapshot.get("build_origin_source") in {"explicit", "confirmed_magics_plate_datum"}
             and finite_number(thickness) and thickness > 0 and finite_number(origin))
    items = []
    for layer, observations in sorted(signals.items()):
        lo = origin + (layer-1)*thickness if known else None
        hi = lo + thickness if known else None
        items.append({"layer": layer, "z_range_mm": [lo, hi] if known else None,
                      "active_bodies": _active_bodies((lo+hi)/2, snapshot.get("geometry_regions") or [], (lo, hi)) if known else [],
                      "observations": observations, "geometry_precision": "sampled_sections" if known else "unknown_origin"})
    return {"status": "ok" if items else "no_observations", "source": "calculated",
            "sample_count": len(items), "items": items[:100],
            "limitations_ru": ["Это участки для сопоставления с контролем качества, а не обнаруженные дефекты.",
                               "Общий датчик не определяет координаты X/Y или виновную деталь; тела перечислены как возможные."]}
