"""Cross-session pattern recognition engine — pure Polars + SciPy.

Four detectors:
  1. detect_signal_trends       — Theil-Sen slope across session means
  2. correlate_events_with_signals — before/after maintenance events
  3. detect_session_anomalies   — modified-z-score outlier sessions
  4. detect_signal_shifts       — step changes (change-point detection, ruptures)

All inputs are small (2–50 sessions with pre-aggregated stats),
so plain Polars expressions are cleaner and faster than embedded SQL.
DuckDB is intentionally not used here; it belongs on raw-file queries
where it can stream TB-scale data without loading everything into memory.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import polars as pl

from analytics.robust_stats import theil_sen_slope
from analytics.telemetry_parser import sessions_to_polars

logger = logging.getLogger(__name__)

MIN_SESSIONS_FOR_TREND   = 3
MIN_SESSIONS_FOR_ANOMALY = 3
MIN_SESSIONS_EACH_SIDE   = 1
SLOPE_THRESHOLD_PCT      = 5.0   # % of mean per session to call it a trend
ZSCORE_THRESHOLD         = 2.5
MIN_SESSIONS_FOR_SHIFT   = 6     # change-point detection needs history on both sides
SHIFT_THRESHOLD_PCT      = 10.0  # % level jump to call it a shift


# ── helpers ──────────────────────────────────────────────────────────────────

def _signal_columns(df: pl.DataFrame) -> list[str]:
    """Return base signal names present in the wide DataFrame."""
    seen: set[str] = set()
    for col in df.columns:
        if "__mean" in col:
            seen.add(col.replace("__mean", ""))
    return sorted(seen)


def _group_for(sessions: list[dict], signal: str) -> str:
    for s in sessions:
        g = (s.get("signal_stats") or {}).get(signal, {}).get("group", "")
        if g:
            return g
    return ""


def _confidence(n_sessions: int, effect_size: float) -> float:
    return round(min(0.95, 0.35 + 0.08 * n_sessions + abs(effect_size) / 200), 2)


# ── 1. Trend detector ────────────────────────────────────────────────────────

def detect_signal_trends(
    sessions: list[dict[str, Any]],
    slope_threshold_pct: float = SLOPE_THRESHOLD_PCT,
) -> list[dict[str, Any]]:
    """Detect signals whose session-mean drifts monotonically across sessions.

    Uses Theil-Sen regression (robust to outlier sessions) via SciPy.
    Applied to each signal's per-session mean in chronological order.
    """
    if len(sessions) < MIN_SESSIONS_FOR_TREND:
        return []

    df = sessions_to_polars(sessions)
    if df.is_empty():
        return []

    findings: list[dict[str, Any]] = []

    for sig in _signal_columns(df):
        col = f"{sig}__mean"
        if col not in df.columns:
            continue

        vals = df[col].drop_nulls().to_numpy()
        n = len(vals)
        if n < MIN_SESSIONS_FOR_TREND:
            continue

        slope = theil_sen_slope(vals)

        overall_mean = float(np.mean(vals))
        # Guard the division: a mean within rounding distance of zero makes
        # "% of mean" meaningless and would manufacture huge phantom trends.
        if abs(overall_mean) < 1e-9:
            continue

        slope_pct = abs(slope / overall_mean * 100)
        if slope_pct < slope_threshold_pct:
            continue

        findings.append({
            "type":              "trend",
            "signal":            sig,
            "group":             _group_for(sessions, sig),
            "direction":         "increasing" if slope > 0 else "decreasing",
            "slope_per_session": round(slope, 6),
            "slope_pct_of_mean": round(slope_pct, 1),
            "n_sessions":        n,
            "mean_overall":      round(overall_mean, 6),
            "sessions":          df["session_id"].to_list(),
            "confidence":        _confidence(n, slope_pct),
        })

    return sorted(findings, key=lambda f: -f["confidence"])


# ── 2. Before / after maintenance events ────────────────────────────────────

def correlate_events_with_signals(
    sessions: list[dict[str, Any]],
    operator_events: list[dict[str, Any]],
    window_sessions: int = 5,
    min_delta_pct: float = 20.0,
) -> list[dict[str, Any]]:
    """Compare signal means before vs after maintenance events.

    For each event timestamp, splits sessions into before/after windows
    using Polars filter expressions and computes the mean change.
    """
    if not operator_events or len(sessions) < 2:
        return []

    df = sessions_to_polars(sessions)
    if df.is_empty():
        return []

    findings: list[dict[str, Any]] = []

    for event in operator_events:
        ets = event.get("timestamp") or event.get("ts") or ""
        if not ets:
            continue
        event_type = event.get("event_type", "unknown")

        for sig in _signal_columns(df):
            col = f"{sig}__mean"
            if col not in df.columns:
                continue

            before_vals = (
                df.filter(pl.col("start_ts") < ets)[col]
                .drop_nulls()
                .tail(window_sessions)
            )
            after_vals = (
                df.filter(pl.col("start_ts") >= ets)[col]
                .drop_nulls()
                .head(window_sessions)
            )

            if len(before_vals) < MIN_SESSIONS_EACH_SIDE:
                continue
            if len(after_vals) < MIN_SESSIONS_EACH_SIDE:
                continue

            b_mean = float(before_vals.mean())
            a_mean = float(after_vals.mean())

            # Guard the division — see detect_signal_trends for rationale.
            if abs(b_mean) < 1e-9:
                continue

            delta_pct = round((a_mean - b_mean) / abs(b_mean) * 100, 1)
            if abs(delta_pct) < min_delta_pct:
                continue

            n_total = len(before_vals) + len(after_vals)
            findings.append({
                "type":        "before_after",
                "event_type":  event_type,
                "signal":      sig,
                "group":       _group_for(sessions, sig),
                "before_mean": round(b_mean, 6),
                "after_mean":  round(a_mean, 6),
                "delta_pct":   delta_pct,
                "n_before":    len(before_vals),
                "n_after":     len(after_vals),
                "event_ts":    ets,
                "confidence":  _confidence(n_total, abs(delta_pct)),
            })

    return sorted(findings, key=lambda f: -abs(f["delta_pct"]))


# ── 3. Outlier session detector ──────────────────────────────────────────────

def detect_session_anomalies(
    sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find sessions whose signal means are outliers vs all other sessions.

    Uses Iglewicz-Hoaglin modified z-score (MAD-based) via Polars Series
    operations — no SQL needed for a handful of rows.
    """
    if len(sessions) < MIN_SESSIONS_FOR_ANOMALY:
        return []

    df = sessions_to_polars(sessions)
    if df.is_empty():
        return []

    findings: list[dict[str, Any]] = []

    for sig in _signal_columns(df):
        col = f"{sig}__mean"
        if col not in df.columns:
            continue

        series = df[col].drop_nulls()
        n = len(series)
        if n < MIN_SESSIONS_FOR_ANOMALY:
            continue

        vals = series.to_numpy()
        med   = float(np.median(vals))
        mad   = float(np.median(np.abs(vals - med)))

        # Parallel arrays for session IDs
        sid_col = (
            df.filter(pl.col(col).is_not_null())["session_id"]
            .to_list()
        )

        group = _group_for(sessions, sig)

        for sid, val in zip(sid_col, vals):
            if mad > 0:
                z = 0.6745 * (val - med) / mad
            else:
                max_dev = float(np.max(np.abs(vals - med)))
                z = (val - med) / (max_dev / 10) if max_dev > 0 else 0.0

            if abs(z) < ZSCORE_THRESHOLD:
                continue

            findings.append({
                "type":          "session_anomaly",
                "session_id":    sid,
                "signal":        sig,
                "group":         group,
                "value":         round(float(val), 6),
                "baseline_mean": round(float(np.mean(vals)), 6),
                "z_score":       round(float(z), 2),
                "direction":     "high" if z > 0 else "low",
                "confidence":    round(min(0.90, 0.4 + abs(z) / 10), 2),
            })

    return sorted(findings, key=lambda f: -abs(f["z_score"]))


# ── Main entry point ─────────────────────────────────────────────────────────

# ── 4. Step-change detector (change-point detection) ────────────────────────

def detect_signal_shifts(
    sessions: list[dict[str, Any]],
    shift_threshold_pct: float = SHIFT_THRESHOLD_PCT,
) -> list[dict[str, Any]]:
    """Detect abrupt level shifts in per-session signal means.

    Complements the Theil-Sen trend detector: a slow drift is a *trend*,
    a sudden jump (filter replaced, recoater knocked, lens contaminated)
    is a *shift*. Uses the ruptures library (PELT, L2 cost).
    """
    if len(sessions) < MIN_SESSIONS_FOR_SHIFT:
        return []
    try:
        import ruptures as rpt
    except ImportError:
        logger.warning("ruptures not installed — shift detection skipped")
        return []

    df = sessions_to_polars(sessions)
    if df.is_empty():
        return []

    findings: list[dict[str, Any]] = []
    session_ids = df["session_id"].to_list()

    for sig in _signal_columns(df):
        col = f"{sig}__mean"
        if col not in df.columns:
            continue
        vals = df[col].drop_nulls().to_numpy()
        if len(vals) < MIN_SESSIONS_FOR_SHIFT:
            continue

        # PELT with L2 cost: optimal segmentation, penalty scaled to variance
        sigma2 = float(np.var(vals)) or 1e-12
        algo = rpt.Pelt(model="l2", min_size=2).fit(vals.reshape(-1, 1))
        breakpoints = algo.predict(pen=3.0 * sigma2)  # BIC-style penalty

        prev = 0
        for bp in breakpoints[:-1]:  # last breakpoint is len(vals)
            before = float(np.mean(vals[prev:bp]))
            after = float(np.mean(vals[bp:]))
            prev = bp
            if abs(before) < 1e-9:
                continue
            jump_pct = abs((after - before) / before * 100)
            if jump_pct < shift_threshold_pct:
                continue
            findings.append({
                "type":             "shift",
                "signal":           sig,
                "group":            _group_for(sessions, sig),
                "direction":        "up" if after > before else "down",
                "at_session":       session_ids[bp] if bp < len(session_ids) else session_ids[-1],
                "session_index":    bp,
                "level_before":     round(before, 6),
                "level_after":      round(after, 6),
                "jump_pct":         round(jump_pct, 1),
                "n_sessions":       len(vals),
                "confidence":       _confidence(len(vals), jump_pct),
            })

    return sorted(findings, key=lambda f: -f["confidence"])


def run_cross_session_analysis(
    sessions: list[dict[str, Any]],
    operator_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all four detectors and return a combined result dict."""
    events     = operator_events or []
    trends     = detect_signal_trends(sessions)
    before_aft = correlate_events_with_signals(sessions, events)
    anomalies  = detect_session_anomalies(sessions)
    shifts     = detect_signal_shifts(sessions)

    n = len(sessions)
    n_findings = len(trends) + len(before_aft) + len(anomalies) + len(shifts)

    if n_findings == 0:
        summary = f"Анализ {n} сессий: отклонений не обнаружено."
    else:
        parts = []
        if trends:
            sigs = ", ".join(f["signal"] for f in trends[:2])
            parts.append(f"тренды по {sigs}")
        if before_aft:
            parts.append(f"{len(before_aft)} корреляций с ТО")
        if anomalies:
            parts.append(f"{len(anomalies)} аномальных сессий")
        if shifts:
            sigs = ", ".join(f["signal"] for f in shifts[:2])
            parts.append(f"ступенчатые сдвиги по {sigs}")
        summary = f"Анализ {n} сессий: {'; '.join(parts)}."

    return {
        "n_sessions_analyzed": n,
        "trends":              trends,
        "before_after":        before_aft,
        "anomalies":           anomalies,
        "shifts":              shifts,
        "summary":             summary,
    }
