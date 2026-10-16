"""Data-reliability assessment for an imported print session.

A companion system must *trust* its inputs before it analyses them: if the
recording contains missing layers or invalid measurements, downstream charts
and predictions need those limitations. A constant signal alone does not
identify a sensor failure. This module runs a set of
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
# usually mean the copy from the machine was incomplete. Additional burn,
# table-temperature and state-flow sources are profile-dependent, not required
# for every print. Their absence must not fail this common minimum check.
_EXPECTED_FAMILIES = {
    SourceFileFamily.main_event_log,
    SourceFileFamily.sensors_log,
    SourceFileFamily.time_log,
}

# Thresholds (deliberately conservative so we flag real problems, not noise).
_LAYER_GAP_FRACTION = 0.02      # >2% of layers missing inside the printed range
_TIME_GAP_MINUTES = 30.0        # descriptive event intervals, not telemetry gaps
_SENSOR_CONSTANT_MIN_N = 50     # describe observed constancy, not a sensor failure
_SENSOR_DROPOUT_FRACTION = 0.5  # signal with <50% of the median sample count

# Severity → score penalty (points subtracted from 100). Counts scale some of
# these up to a capped maximum so one catastrophic issue can dominate the score.
_PENALTY = {
    "missing_log_family": 15.0,
    "layer_gaps":         20.0,
    "sensor_dropout":     10.0,
    "parse_warnings":      8.0,
}


def _normalize_dt(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _issue(kind: str, severity: str, detail: str, count: int = 1, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "detail": detail, "count": count, **extra}


def _check_layer_continuity(layers: list[int]) -> dict[str, Any] | None:
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
    actual print window — same rationale as session_overview.compute_session_spans.
    """
    times: list[datetime] = []
    for f in files:
        pr = getattr(f, "parse_result", None)
        if not pr or pr.file_family == SourceFileFamily.monitor100_log:
            continue
        times.extend(_normalize_dt(e.ts) for e in pr.events if e.ts is not None)
        times.extend(_normalize_dt(t.ts_start) for t in pr.transitions if t.ts_start is not None)
    return sorted(times)


def _check_time_continuity(times: list[datetime]) -> dict[str, Any] | None:
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
        "event_gaps", "info",
        f"{len(gaps)} интервал(ов) между событиями > {_TIME_GAP_MINUTES:.0f} мин "
        f"(макс. {worst_min:.0f} мин); это не подтверждает потерю измерений датчиков",
        count=len(gaps), max_gap_min=round(worst_min, 1),
        gap_start=worst[0].isoformat(), gap_end=worst[1].isoformat(),
    )


def _check_sensors(signal_stats: dict[str, Any]) -> list[dict[str, Any]]:
    """Observed constancy and missing samples are different kinds of evidence."""
    issues: list[dict[str, Any]] = []
    if not signal_stats:
        return issues

    counts = [s.get("n", 0) for s in signal_stats.values() if isinstance(s.get("n"), int)]
    median_n = median(counts) if counts else 0

    constant: list[str] = []
    dropped: list[str] = []
    for sig, s in signal_stats.items():
        n = s.get("n", 0)
        std = s.get("std")
        if isinstance(n, int) and n >= _SENSOR_CONSTANT_MIN_N and isinstance(std, (int, float)) and std == 0:
            constant.append(sig)
        if median_n and isinstance(n, int) and n < median_n * _SENSOR_DROPOUT_FRACTION:
            dropped.append(sig)

    if constant:
        issues.append(_issue(
            "constant_signals", "info",
            f"Постоянное значение каналов: {', '.join(sorted(constant))}; "
            "само по себе не подтверждает зависание датчика",
            count=len(constant), signals=sorted(constant),
        ))
    if dropped:
        issues.append(_issue(
            "sensor_dropout", "medium",
            f"Канал(ы) с резко меньшим числом пригодных отсчётов: "
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
        malformed += pr.malformed_row_count
        for table in pr.tables:
            unknown_cols.update(getattr(table, "unknown_columns", []) or [])
        for diag in pr.diagnostics:
            if getattr(diag, "severity", "") in ("warning", "error"):
                code = getattr(diag, "code", "") or "unknown"
                # The aggregate is a storage marker for the same row finding,
                # not another category of unreliable data.
                if code == "malformed_stateflow_rows_aggregated":
                    code = "malformed_stateflow_row"
                warn_codes.add(code)
    if not malformed and not unknown_cols and not warn_codes:
        return None
    parts = []
    if malformed:
        parts.append(f"{malformed} строк с нарушенным форматом или значениями")
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

    Returns ``{score, grade, issues, diagnostics, checks}`` where ``score`` is
    0..100 when observations exist (100 = no penalized findings), or None when
    there is nothing to assess. ``issues`` lists actionable problems; ``checks``
    records which checks ran (for transparency / debugging).
    """
    signal_stats = signal_stats or {}
    issues: list[dict[str, Any]] = []
    layers = sorted({event.layer for event in events
                     if isinstance(getattr(event, "layer", None), int) and event.layer >= 0})
    event_times = _print_event_times(files)

    for check in (
        _check_missing_families(files),
        _check_layer_continuity(layers),
        _check_time_continuity(event_times),
        _check_parse_diagnostics(files),
    ):
        if check:
            issues.append(check)
    issues.extend(_check_sensors(signal_stats))
    diagnostics = [item for item in issues if item["severity"] == "info"]
    issues = [item for item in issues if item["severity"] != "info"]

    # Score: subtract capped penalties. A count amplifies the base penalty up to
    # 2x so several occurrences hurt more than one, but a single kind can't sink
    # the score on its own beyond its cap.
    total_penalty = 0.0
    for issue in issues:
        base = _PENALTY.get(issue["kind"], 5.0)
        amp = min(2.0, 1.0 + math.log10(max(1, issue.get("count", 1))))
        total_penalty += base * amp
    has_sensor_samples = any(isinstance(stats.get("n"), int) and stats["n"] > 0
                             for stats in signal_stats.values())
    has_observations = bool(layers or event_times or has_sensor_samples)
    score = round(max(0.0, 100.0 - total_penalty), 1) if has_observations else None
    grade = "unknown" if score is None else "good" if score >= 85 else "fair" if score >= 60 else "poor"

    return {
        "score": score,
        "grade": grade,
        "issues": issues,
        "diagnostics": diagnostics,
        "checks": {
            "layers": len(layers) >= 2,
            "time": len(event_times) >= 3,
            "sensors": has_sensor_samples,
            "parse_diagnostics": True,
            "missing_families": True,
        },
    }


__all__ = ["assess_session_quality"]
