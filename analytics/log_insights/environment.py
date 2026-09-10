"""Streaming exposure integrals and post-pause recovery, without filling gaps."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from analytics.log_insights.clocks import iso, seconds
from analytics.prediction.timing_validation import finite_number
from parsers.common.encoding import estimate_encoding, iter_text_lines
from parsers.common.timestamps import date_hint_from_filename, parse_timestamp_token
from profiles.signal_catalog import load_signal_catalog, signal_display_name


def default_thresholds():
    """Profile limits are reference values, not a validated material recipe."""
    return {
        key: {"high": row["alarm_high"], "unit": row.get("unit", ""),
              "confirmed": False, "origin": "machine_profile_candidate"}
        for key, row in load_signal_catalog().items()
        if row.get("semantic_class") in {"oxygen", "temperature", "humidity"}
        and finite_number(row.get("alarm_high"))
    }


def sensor_samples(files, thresholds, diagnostics, clock_timezone="Europe/Moscow"):
    """Yield (absolute seconds, aligned values), retaining missing cells as None."""
    catalog = load_signal_catalog()
    paths = {}
    for source in files:
        if str(source.classification.family) != "sensors_log":
            continue
        paths.setdefault(source.checksum or source.path, Path(source.path))
    def date_key(path):
        try:
            hint = date_hint_from_filename(path)
            return str(hint or ""), str(path)
        except ValueError:
            return "", str(path)

    for path in sorted(paths.values(), key=date_key):
        if not path.is_file():
            diagnostics["missing_files"] += 1
            continue
        try:
            hint = date_hint_from_filename(path)
        except ValueError:
            hint = None
        columns, previous, shift = {}, None, 0
        for _, _, line in iter_text_lines(path, estimate_encoding(path)):
            cells = [c.strip() for c in line.split("|")]
            if "Time" in cells:
                columns = {key: i for i, key in enumerate(cells)}
                continue
            if "Time" not in columns or columns["Time"] >= len(cells):
                continue
            raw_clock = cells[columns["Time"]]
            stamp, _, _ = parse_timestamp_token(raw_clock, hint)
            if stamp is None:
                diagnostics["invalid_timestamps"] += 1
                continue
            # Daily clock-only logs may run through midnight within one file.
            clock_only = len(raw_clock) <= 16
            adjusted = stamp + timedelta(days=shift if clock_only else 0)
            if clock_only and previous and adjusted < previous - timedelta(hours=12):
                shift += 1
                adjusted += timedelta(days=1)
                diagnostics["midnight_rollovers"] += 1
            previous = adjusted
            values = {}
            for key in thresholds:
                idx = columns.get(key)
                if idx is None:
                    continue
                value = None
                if idx is not None and idx < len(cells):
                    try:
                        value = float(cells[idx].replace(",", "."))
                    except ValueError:
                        pass
                rule = catalog.get(key, {})
                if (not finite_number(value) or abs(value) > 1e7
                        or value in rule.get("invalid_values", [])
                        or (rule.get("min_val") is not None and value < rule["min_val"])
                        or (rule.get("max_val") is not None and value > rule["max_val"])):
                    value = None
                    diagnostics[f"invalid_cells:{key}"] += 1
                values[key] = value
            yield seconds(adjusted, clock_timezone), values


def _excess_segment(left, right, duration, threshold):
    """Exact integral of the positive part of a linear segment."""
    a, b = left - threshold, right - threshold
    if a <= 0 and b <= 0:
        return 0.0, 0.0
    if a >= 0 and b >= 0:
        return duration, duration * (a + b) / 2
    fraction = max(a, b) / abs(b - a)
    above = duration * fraction
    return above, above * max(a, b) / 2


def analyze_environment(samples, windows, pauses, thresholds, *, max_gap_s=5.0,
                        stable_s=30.0, recovery_window_s=600.0, detail_limit=100,
                        required_signals=None):
    starts = [row["start"] for row in windows]
    totals = defaultdict(lambda: {"observed_seconds": 0.0, "exceedance_seconds": 0.0,
                                  "excess_integral": 0.0, "value_integral": 0.0, "sample_segments": 0})
    layers = defaultdict(lambda: defaultdict(lambda: {"observed_seconds": 0.0,
                         "exceedance_seconds": 0.0, "excess_integral": 0.0, "value_integral": 0.0}))
    recovery = [{"resume": p["end"], "stable_from": None, "recovery_seconds": None,
                 "observed_samples": 0, "status": "insufficient_data", "last": None}
                for p in pauses if p["resumed"] and p["end"] is not None]
    previous = None
    rejected_gaps = out_of_order = sample_count = 0
    for timestamp, values in samples:
        sample_count += 1
        if previous is not None and timestamp <= previous[0]:
            out_of_order += 1
            continue
        for item in recovery:
            if not item["resume"] <= timestamp <= item["resume"] + recovery_window_s:
                continue
            item["observed_samples"] += 1
            required = tuple(required_signals) if required_signals is not None else tuple(key for key in thresholds if key in values)
            valid = bool(required) and all(finite_number(values.get(key)) for key in required)
            good = valid and all(values[key] <= thresholds[key]["high"] for key in required)
            if item.get("signals") != required:
                item["stable_from"] = None
                item["signals"] = required
            contiguous = item["last"] is not None and timestamp - item["last"] <= max_gap_s
            if not good:
                item["stable_from"] = None
            elif item["stable_from"] is None or not contiguous:
                item["stable_from"] = timestamp
            if item["recovery_seconds"] is None and item["stable_from"] is not None:
                if timestamp - item["stable_from"] >= stable_s:
                    item["recovery_seconds"] = item["stable_from"] - item["resume"]
                    item["status"] = "stable_observed"
            item["last"] = timestamp
        if previous is not None:
            t0, v0 = previous
            dt = timestamp - t0
            if dt > max_gap_s:
                rejected_gaps += 1
            else:
                index = max(0, bisect_right(starts, t0) - 1)
                while index < len(windows) and windows[index]["start"] < timestamp:
                    window = windows[index]
                    lo, hi = max(t0, window["start"]), min(timestamp, window["end"])
                    if hi > lo:
                        for key, rule in thresholds.items():
                            a, b = v0.get(key), values.get(key)
                            if not (finite_number(a) and finite_number(b)):
                                continue
                            left, right = a + (b-a)*(lo-t0)/dt, a + (b-a)*(hi-t0)/dt
                            above, integral = _excess_segment(left, right, hi-lo, rule["high"])
                            for target in (totals[key], layers[window["layer"]][key]):
                                target["observed_seconds"] += hi-lo
                                target["exceedance_seconds"] += above
                                target["excess_integral"] += integral
                                target["value_integral"] += (left + right) * (hi-lo) / 2
                            totals[key]["sample_segments"] += 1
                    index += 1
        previous = timestamp, values
    expected = sum(w["end"] - w["start"] for w in windows)
    metrics = [{"signal": key, "name_ru": signal_display_name(key), **data,
                "time_weighted_mean": data["value_integral"] / data["observed_seconds"],
                "threshold": thresholds[key], "integral_unit": thresholds[key].get("unit", "") + "·s",
                "coverage_ratio": data["observed_seconds"] / expected if expected else None}
               for key, data in totals.items()]
    items = [{"layer": layer, "signals": dict(signals)} for layer, signals in layers.items()
             if any(row["exceedance_seconds"] > 0 for row in signals.values())]
    # Different units cannot be ranked by their raw integrals; rank by exposure time.
    items.sort(key=lambda item: max(r["exceedance_seconds"] for r in item["signals"].values()), reverse=True)
    for item in recovery:
        if item["status"] == "insufficient_data" and item["observed_samples"] >= 2:
            item["status"] = "stability_not_established"
        item["resume_timestamp"] = iso(item.pop("resume"))
        item.pop("stable_from")
        item.pop("last")
    return {
        "status": "ok" if metrics else "insufficient_data", "source": "calculated",
        "sample_count": sample_count, "metrics": metrics,
        "layer_items": items[:detail_limit], "affected_layer_count": len(items),
        "details_truncated": len(items) > detail_limit,
        "burn_window_count": len(windows), "burn_window_seconds": expected,
        "approximate_window_count": sum(w["precision"] != "logged_start" for w in windows),
        "rejected_gaps": rejected_gaps, "out_of_order_rows": out_of_order,
        "recovery": {"items": recovery[:detail_limit], "count": len(recovery), "stable_window_seconds": stable_s,
                     "source": "calculated", "sample_count": sum(r["observed_samples"] for r in recovery),
                     "limitations_ru": ["Стабильность показаний не подтверждает исправность датчиков; проверяются доступные каналы либо весь явно заданный набор."]},
        "limitations_ru": [
            "Интеграл рассчитан линейно между соседними отсчётами; большие пропуски исключены.",
            "Порог профиля не является подтверждённым режимом материала, пока не отмечен как confirmed.",
            "Привязка по общей метке прожига приблизительна; причина отклонения и брак не устанавливаются.",
        ],
    }
