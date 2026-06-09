"""Data-reliability assessment for an imported print session.

A companion system must *trust* its inputs before it analyses them: if the
printer skipped layers, the recording was interrupted, or a sensor froze, the
downstream charts and predictions are built on sand. This module runs a set of
cheap, pure checks over the *already-parsed* session data (no re-reading of raw
logs) and produces a 0..100 ``data_quality_score`` plus an explicit list of
issues an operator can act on.

Design mirrors ``analytics/process_health.py``: pure functions over the compact
structures produced at ingest, storage-agnostic, no external I/O.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import median
from typing import Any

from domain.enums.common import SourceFileFamily

# Log families a genuine print run must yield AFTER ingestion. Missing ones
# usually mean the copy from the machine was incomplete. Note: burn.log /
# table_temp.log / stateFlow* are intentionally skipped by IngestionService as
# redundant (their data lives in sensors.log + time.log), so they are NOT
# expected here — including them would false-positive on every session.
_EXPECTED_FAMILIES = {
    SourceFileFamily.main_event_log,
    SourceFileFamily.sensors_log,
    SourceFileFamily.time_log,
}

# Thresholds (deliberately conservative so we flag real problems, not noise).
_LAYER_GAP_FRACTION = 0.02      # >2% of layers missing inside the printed range
_TIME_GAP_MINUTES = 30.0        # a recording gap larger than this is suspicious
_SENSOR_STUCK_MIN_N = 50        # need enough points before "std==0" means frozen
_SENSOR_DROPOUT_FRACTION = 0.5  # signal with <50% of the median sample count

# Severity → score penalty (points subtracted from 100). Counts scale some of
# these up to a capped maximum so one catastrophic issue can dominate the score.
_PENALTY = {
    "missing_log_family": 15.0,
    "layer_gaps":         20.0,
    "time_gaps":          12.0,
    "sensor_stuck":       10.0,
    "sensor_dropout":     10.0,
    "parse_warnings":      8.0,
}


def _normalize_dt(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _issue(kind: str, severity: str, detail: str, count: int = 1, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "detail": detail, "count": count, **extra}


def _check_layer_continuity(events: list[Any]) -> dict[str, Any] | None:
    layers = sorted({
        e.layer for e in events
        if isinstance(getattr(e, "layer", None), int) and e.layer >= 0
    })
    if len(layers) < 2:
        return None
    span = layers[-1] - layers[0] + 1
    missing = span - len(layers)
    if missing <= 0 or missing / span < _LAYER_GAP_FRACTION:
        return None
    # Largest single run of consecutive missing layers (most diagnostic).
    largest_gap = max(b - a - 1 for a, b in zip(layers, layers[1:]))
    return _issue(
        "layer_gaps", "high" if missing / span > 0.1 else "medium",
        f"Пропущено {missing} из {span} слоёв в диапазоне "
        f"{layers[0]}–{layers[-1]} (макс. подряд {largest_gap})",
        count=missing, layer_min=layers[0], layer_max=layers[-1],
        largest_gap=largest_gap, fraction=round(missing / span, 4),
    )


def _print_event_times(files: list[Any]) -> list[datetime]:
    """Sorted event/transition timestamps, excluding the monitor100 daemon log.

    monitor100 runs continuously and would inject spurious 'gaps' around the
    actual print window — same rationale as session_overview.compute_print_span.
    """
    times: list[datetime] = []
    for f in files:
        pr = getattr(f, "parse_result", None)
        if not pr or pr.file_family == SourceFileFamily.monitor100_log:
            continue
        times.extend(_normalize_dt(e.ts) for e in pr.events if e.ts is not None)
        times.extend(_normalize_dt(t.ts_start) for t in pr.transitions if t.ts_start is not None)
    return sorted(times)


def _check_time_continuity(files: list[Any]) -> dict[str, Any] | None:
    times = _print_event_times(files)
    if len(times) < 3:
        return None
    threshold = _TIME_GAP_MINUTES * 60.0
    gaps = [
        (a, b, (b - a).total_seconds())
        for a, b in zip(times, times[1:])
        if (b - a).total_seconds() > threshold
    ]
    if not gaps:
        return None
    worst = max(gaps, key=lambda g: g[2])
    worst_min = worst[2] / 60.0
    return _issue(
        "time_gaps", "high" if worst_min > 120 else "medium",
        f"{len(gaps)} разрыв(ов) в записи > {_TIME_GAP_MINUTES:.0f} мин "
        f"(макс. {worst_min:.0f} мин) — возможен обрыв логирования",
        count=len(gaps), max_gap_min=round(worst_min, 1),
        gap_start=worst[0].isoformat(), gap_end=worst[1].isoformat(),
    )


def _check_sensors(signal_stats: dict[str, Any]) -> list[dict[str, Any]]:
    """Frozen (std==0) and dropped-out (too few samples) sensors."""
    issues: list[dict[str, Any]] = []
    if not signal_stats:
        return issues

    counts = [s.get("n", 0) for s in signal_stats.values() if isinstance(s.get("n"), int)]
    median_n = median(counts) if counts else 0

    stuck: list[str] = []
    dropped: list[str] = []
    for sig, s in signal_stats.items():
        n = s.get("n", 0)
        std = s.get("std")
        if isinstance(n, int) and n >= _SENSOR_STUCK_MIN_N and isinstance(std, (int, float)) and std == 0:
            stuck.append(sig)
        elif median_n and isinstance(n, int) and n < median_n * _SENSOR_DROPOUT_FRACTION:
            dropped.append(sig)

    if stuck:
        issues.append(_issue(
            "sensor_stuck", "medium",
            f"Датчик(и) с постоянным значением (возможно завис): {', '.join(sorted(stuck))}",
            count=len(stuck), signals=sorted(stuck),
        ))
    if dropped:
        issues.append(_issue(
            "sensor_dropout", "medium",
            f"Датчик(и) с резко меньшим числом отсчётов (возможен обрыв): "
            f"{', '.join(sorted(dropped))}",
            count=len(dropped), signals=sorted(dropped),
        ))
    return issues


def _check_parse_diagnostics(files: list[Any]) -> dict[str, Any] | None:
    malformed = 0
    unknown_cols: set[str] = set()
    warn_codes: set[str] = set()
    for f in files:
        pr = getattr(f, "parse_result", None)
        if not pr:
            continue
        for table in pr.tables:
            malformed += getattr(table, "malformed_rows", 0) or 0
            unknown_cols.update(getattr(table, "unknown_columns", []) or [])
        for diag in pr.diagnostics:
            if getattr(diag, "severity", "") in ("warning", "error"):
                warn_codes.add(getattr(diag, "code", "") or "unknown")
    if not malformed and not unknown_cols and not warn_codes:
        return None
    parts = []
    if malformed:
        parts.append(f"{malformed} битых строк")
    if unknown_cols:
        parts.append(f"{len(unknown_cols)} неизвестных колонок")
    if warn_codes:
        parts.append(f"коды: {', '.join(sorted(warn_codes))}")
    return _issue(
        "parse_warnings", "low",
        "Предупреждения парсеров — " + "; ".join(parts),
        count=malformed or len(unknown_cols) or len(warn_codes),
        malformed_rows=malformed, unknown_columns=sorted(unknown_cols),
        codes=sorted(warn_codes),
    )


def _check_missing_families(files: list[Any]) -> dict[str, Any] | None:
    present = {
        f.parse_result.file_family
        for f in files
        if getattr(f, "parse_result", None) is not None
    }
    # Only judge completeness for things that clearly are print sessions: at
    # least two expected families present. Otherwise (stray single file) skip.
    if len(present & _EXPECTED_FAMILIES) < 2:
        return None
    missing = _EXPECTED_FAMILIES - present
    if not missing:
        return None
    names = sorted(m.value for m in missing)
    return _issue(
        "missing_log_family", "high" if len(missing) > 1 else "medium",
        f"Отсутствуют ожидаемые логи печати: {', '.join(names)} "
        "(неполная выгрузка с машины?)",
        count=len(missing), families=names,
    )


def assess_session_quality(
    files: list[Any],
    events: list[Any],
    telemetry: dict[str, Any] | None = None,
    signal_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess input-data reliability for one grouped session.

    Returns ``{score, grade, issues, checks}`` where ``score`` is 0..100
    (100 = clean), ``issues`` is a list of actionable problems, and ``checks``
    records which checks ran (for transparency / debugging).
    """
    signal_stats = signal_stats or {}
    issues: list[dict[str, Any]] = []

    for check in (
        _check_missing_families(files),
        _check_layer_continuity(events),
        _check_time_continuity(files),
        _check_parse_diagnostics(files),
    ):
        if check:
            issues.append(check)
    issues.extend(_check_sensors(signal_stats))

    # Score: subtract capped penalties. A count amplifies the base penalty up to
    # 2x so several occurrences hurt more than one, but a single kind can't sink
    # the score on its own beyond its cap.
    total_penalty = 0.0
    for issue in issues:
        base = _PENALTY.get(issue["kind"], 5.0)
        amp = min(2.0, 1.0 + math.log10(max(1, issue.get("count", 1))))
        total_penalty += base * amp
    score = round(max(0.0, 100.0 - total_penalty), 1)
    grade = "good" if score >= 85 else "fair" if score >= 60 else "poor"

    return {
        "score": score,
        "grade": grade,
        "issues": issues,
        "checks": {
            "layers": True,
            "time": True,
            "sensors": bool(signal_stats),
            "parse_diagnostics": True,
            "missing_families": True,
        },
    }


__all__ = ["assess_session_quality"]
