"""Read-only comparison of like-for-like published print time scopes.

Only shared compact timing evidence is read. Missing evidence never causes a
raw-file parse, a wall-clock fallback, or an estimate to validate itself.
"""
from collections import defaultdict
from copy import deepcopy

from sqlalchemy import select

from analytics.prediction.accuracy import PRINT_CLASSIFICATIONS, as_utc
from analytics.prediction.timing_snapshot import (
    MANIFEST_KEY, published_timing_events, timing_publication_status,
)
from analytics.prediction.timing_validation import (
    calibration_cycles_ms, finite_number, has_complete_layer_coverage, timing_components_ms,
)
from domain.models.events import LayerSnapshot
from domain.models.sessions import BuildSession


_REASONS = {
    "no_prediction": "Нет сохранённого прогноза для сравнения.",
    "invalid_prediction": "Сохранённое время прогноза некорректно; нужен новый расчёт.",
    "no_session": "К карточке не привязаны доступные логи.",
    "not_a_print": "Сессия не подтверждена как реальная печать.",
    "timing_unavailable": "Нет проверяемых сохранённых измерений времени слоёв.",
    "coverage_unknown": "Неизвестно ожидаемое число слоёв; полнота логов не подтверждена.",
    "partial_coverage": "Измерения не покрывают все ожидаемые слои; сумма показана только в диагностике.",
    "normal_cycle_unavailable": "Полный нормальный цикл без пауз не подтверждён опубликованным анализом.",
    "comparable": None,
}


def _number(value, *, positive=False):
    return float(value) if finite_number(value) and (value > 0 if positive else value >= 0) else None


def _hours(seconds):
    value = _number(seconds)
    return round(value / 3600, 3) if value is not None else None


def comparison_summary(record, session=None, rows=()):
    """Pure projection over detached SQL values, with explicit unknown states."""
    snapshot = (record.get("metadata_json") or {}).get("prediction") or {}
    scope = ("machine_cycle" if "machine_cycle_hours" in snapshot
             else "burn_plus_pour" if "print_hours" in snapshot else None)
    predicted = _number(snapshot.get("machine_cycle_hours" if scope == "machine_cycle" else "print_hours"), positive=True)
    group = (session or {}).get("group") or {}
    analysis = group.get("analysis_snapshot") or {}
    features = analysis.get("features") or group.get("features") or {}
    accounting = analysis.get("time_accounting") or {}
    explicit_pause = accounting.get("explicit_pause_seconds", features.get("explicit_pause_seconds"))
    expected = snapshot.get("layer_count")
    expected = expected if type(expected) is int and expected > 0 else None
    manifest = (session or {}).get("manifest")
    publication = timing_publication_status(list(rows), manifest)
    events = published_timing_events(list(rows), manifest) or []
    components, cycles = timing_components_ms(events), calibration_cycles_ms(events)
    selected = cycles if scope == "machine_cycle" else components
    complete = has_complete_layer_coverage(selected, expected)
    subtotal_ms = sum(burn + pour for burn, pour in components.values()) if components else None
    cycle_ms = sum(make for _, _, make in cycles.values()) if cycles else None
    actual = source = None
    if scope is None:
        status = "no_prediction"
    elif predicted is None:
        status = "invalid_prediction"
    elif session is None:
        status = "no_session"
    elif (group.get("classification") or session.get("classification")) not in PRINT_CLASSIFICATIONS:
        status = "not_a_print"
    elif publication in {"invalid", "absent", "empty", "no_time_log"} or not selected:
        status = "timing_unavailable"
    elif expected is None:
        status = "coverage_unknown"
    elif not complete:
        status = "partial_coverage"
    elif scope == "burn_plus_pour":
        actual, source, status = subtotal_ms / 3_600_000, "subtotal_machine_log", "comparable"
    else:
        # This independently published normal value must cover the SAME complete
        # timing generation, not merely some eligible observed layers. Never
        # apply today's predicted overhead/floor to construct its own target.
        normal = _number(accounting.get("normal_unique_layer_seconds"), positive=True)
        same_generation = (
            publication == "complete"
            and group.get("timing_publication_id") == manifest.get("publication_id")
            and analysis.get("schema_version") == 1 and bool(analysis.get("analysis_id"))
        )
        usable = (
            same_generation and normal is not None
            and accounting.get("status") == "ok"
            and accounting.get("normal_layer_count") == expected
            and accounting.get("source") in {"calculated", "calibrated"}
            and accounting.get("normal_time_scope") in {"eligible_measured_layers_only", "complete_print"}
            and subtotal_ms / 1000 <= normal <= cycle_ms / 1000 + 0.02 * expected
        )
        if usable:
            actual, source, status = normal / 3600, "normal_machine_log", "comparable"
        else:
            status = "normal_cycle_unavailable"
    wall = None
    if session is not None and session.get("start_ts") and session.get("end_ts"):
        wall = _hours((as_utc(session["end_ts"]) - as_utc(session["start_ts"])).total_seconds())
    return {
        "contract_version": 2,
        "comparison_scope": scope,
        "comparison_status": status,
        "comparison_reason_ru": _REASONS[status],
        "predicted_hours": round(predicted, 3) if predicted is not None else None,
        "predicted_cost_rub": snapshot.get("cost_total_rub"),
        "actual_hours": round(actual, 3) if actual is not None else None,
        "actual_source": source,
        "idle_hours": _hours(explicit_pause),
        "layers": features.get("layers"),
        "error_pct": round((predicted - actual) / actual * 100, 1) if actual and predicted else None,
        "coverage": {"expected_layers": expected, "measured_layers": len(selected), "complete": complete},
        "diagnostics": {
            "wall_clock_hours": wall,
            "measured_burn_plus_pour_hours": _hours(subtotal_ms / 1000) if subtotal_ms is not None else None,
            "measured_cycle_hours": _hours(cycle_ms / 1000) if cycle_ms is not None else None,
        },
    }


def attach_plan_vs_fact(repo, records):
    """Batch-read page evidence, close SQL, then derive summaries locally.

    Pass a clean read session: this boundary never commits caller mutations.
    """
    ids = sorted({record["session_id"] for record in records if record.get("session_id")})
    sessions, timings = {}, defaultdict(list)
    try:
        if ids:
            for row in repo.db.execute(select(
                BuildSession.session_id, BuildSession.start_ts, BuildSession.end_ts,
                BuildSession.classification,
                BuildSession.context["runtime_payload"]["group"]["classification"].as_string().label("group_classification"),
                BuildSession.context["runtime_payload"]["group"]["features"].label("features"),
                BuildSession.context["runtime_payload"]["group"]["analysis_snapshot"].label("analysis_snapshot"),
                BuildSession.context["runtime_payload"]["group"]["timing_publication_id"].as_string().label("timing_publication_id"),
                BuildSession.context[MANIFEST_KEY].label("manifest"),
            ).where(BuildSession.session_id.in_(ids))):
                values = deepcopy(dict(row._mapping))
                values["group"] = {"classification": values.pop("group_classification"),
                                   **{key: values.pop(key) for key in (
                                       "features", "analysis_snapshot", "timing_publication_id")}}
                sessions[row.session_id] = values
            for row in repo.db.execute(select(
                LayerSnapshot.session_id, LayerSnapshot.layer, LayerSnapshot.features,
                LayerSnapshot.context["publication_id"].as_string(),
            ).where(LayerSnapshot.session_id.in_(ids))):
                timings[row[0]].append(deepcopy(tuple(row[1:])))
    finally:
        repo.db.rollback()
    for record in records:
        sid = record.get("session_id")
        record["summary"] = comparison_summary(record, sessions.get(sid), timings.get(sid, []))
