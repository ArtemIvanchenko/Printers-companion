"""Full-resolution sensors.log parser using Polars + SciPy.

Reads the complete pipe-delimited sensors log (typically 330k–400k rows)
and computes comprehensive per-signal statistics.  Called once at import time;
results are stored in session.context so the raw file is never needed again.

Dependencies already in pyproject.toml: polars>=0.20, scipy>=1.13.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from analytics.robust_stats import theil_sen_slope

logger = logging.getLogger(__name__)

# ── Signal → semantic group mapping ─────────────────────────────────────────

_GROUP: dict[str, str] = {
    # Oxygen
    "SO1": "oxygen",      "SO2": "oxygen",
    # Temperatures
    "ST3": "temperature", "ST4": "temperature", "ST5": "temperature",
    "ST2": "temperature",                         # recuperator zone
    # Pressure — confirmed in sensors.log header
    "SP1": "pressure",    "SP2": "pressure",      "SP3": "pressure",
    "SP4": "pressure",    "SP5": "pressure",      "SP8": "pressure",
    "SP14": "pressure",   "SP15": "pressure",     "SP16": "pressure",
    # Pressure — SCADA visible but may appear in other sessions
    "SP9": "pressure",    "SP11": "pressure",     "SP12": "pressure",
    # Flow
    "SF1": "flow",
    # Gas flow temperature / humidity (two column-name variants in use)
    "ST1 (flow H)": "humidity",  "Flow H": "humidity",
    "ST1 (flow T)": "temp_gas",  "Flow T": "temp_gas",
    # Mechanics / counters
    "LIR": "layer_counter",
    # Binary / other signals seen on SCADA
    "BI1": "binary",
}

# Columns that are always numeric; "Time" stays as string.
_NUMERIC_COLS = set(_GROUP.keys()) | {"Raquel", "Bunker", "Filled B"}


def parse_sensors_log(path: Path) -> dict[str, np.ndarray]:
    """Stream the sensors.log line-by-line, accumulating only analytics signals.

    Uses ``array.array('f')`` (Float32) for minimal peak RAM:
    ≈ 4 bytes/value × 334k rows × 9 signals ≈ 12 MB.
    Returns a dict {signal_name: np.ndarray(float64)} ready for stats.
    """
    import array as _array

    buffers: dict[str, _array.array] = {}
    col_idx: dict[str, int] = {}

    with path.open(encoding="utf-8", errors="replace", buffering=1 << 20) as fh:
        # Header line: map stripped column names to their pipe-column index.
        header_line = fh.readline()
        parts = [p.strip() for p in header_line.split("|")]
        for i, name in enumerate(parts):
            if name in _GROUP:
                col_idx[name] = i
                buffers[name] = _array.array("f")

        if not col_idx:
            return {}

        # Data lines: parse relevant columns only.
        for line in fh:
            cells = line.split("|")
            for sig, idx in col_idx.items():
                if idx >= len(cells):
                    continue
                raw = cells[idx].strip().replace(",", ".")
                try:
                    buffers[sig].append(float(raw))
                except (ValueError, OverflowError):
                    pass  # null / header repeat / garbage line

    return {sig: np.array(buf, dtype=np.float64)
            for sig, buf in buffers.items() if buf}


def compute_full_signal_stats(
    path: Path,
    alarm_thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute per-signal statistics from the complete sensors.log.

    Args:
        path: absolute path to the *_sensors.log file.
        alarm_thresholds: optional dict {signal: {"alarm_high": float,
                          "alarm_low": float}} from signals.yaml.

    Returns:
        {signal: {mean, std, min, max, p05, p95, p99, n,
                  alarm_count, trend_slope, group}}

    ``trend_slope`` is the Theil-Sen slope in *signal units per row*
    (≈ per second for 1-Hz logs), positive = rising over the session.
    """
    try:
        arrays = parse_sensors_log(path)
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", path, exc)
        return {}

    thresholds = alarm_thresholds or {}
    result: dict[str, dict[str, Any]] = {}

    for col, vals in arrays.items():
        n = len(vals)
        if n < 10:
            continue

        # Robust linear trend within the session. The shared helper sub-samples
        # large arrays before the O(n²) regression (334k rows would take hours).
        slope_val = theil_sen_slope(vals)

        # Alarm count (above alarm_high OR below alarm_low).
        thr = thresholds.get(col, {})
        alarm_count = 0
        if (ah := thr.get("alarm_high")) is not None:
            alarm_count += int(np.sum(vals > ah))
        if (al := thr.get("alarm_low")) is not None:
            alarm_count += int(np.sum(vals < al))

        result[col] = {
            "mean":        round(float(vals.mean()), 6),
            "std":         round(float(vals.std()),  6),
            "min":         round(float(vals.min()),  6),
            "max":         round(float(vals.max()),  6),
            "p05":         round(float(np.quantile(vals, 0.05)), 6),
            "p95":         round(float(np.quantile(vals, 0.95)), 6),
            "p99":         round(float(np.quantile(vals, 0.99)), 6),
            "n":           n,
            "alarm_count": alarm_count,
            "trend_slope": round(slope_val, 8),
            "group":       _GROUP[col],
        }

    return result


def sessions_to_polars(sessions: list[dict[str, Any]]) -> pl.DataFrame:
    """Convert a list of session dicts (with signal_stats) to a wide Polars DataFrame.

    One row per session, columns: session_id, start_ts, then for each signal
    the stats suffixed: SO1_mean, SO1_std, SO1_p95, SO1_alarm_count, etc.

    Used by the cross-session analysis engine.
    """
    rows: list[dict[str, Any]] = []
    for s in sessions:
        row: dict[str, Any] = {
            "session_id": s.get("session_id", ""),
            "start_ts":   s.get("start_ts") or "",
        }
        for sig, stats in (s.get("signal_stats") or {}).items():
            for metric in ("mean", "std", "p95", "p99", "alarm_count", "trend_slope"):
                row[f"{sig}__{metric}"] = stats.get(metric)
        rows.append(row)

    if not rows:
        return pl.DataFrame()

    return pl.DataFrame(rows).sort("start_ts")
