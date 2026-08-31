"""Small deterministic process-mining layer over canonical printer events."""

from __future__ import annotations

from collections import Counter
from typing import Any


_PHASE_NAMES_RU = {
    "preparation": "Подготовка",
    "laser_scan": "Лазерное сканирование",
    "powder_recoat": "Нанесение порошка",
    "pause": "Пауза",
    "alarm": "Авария или предупреждение",
    "completion": "Завершение",
}

_ALLOWED_TRANSITIONS = {
    ("preparation", "laser_scan"),
    ("laser_scan", "powder_recoat"),
    ("powder_recoat", "laser_scan"),
    ("laser_scan", "completion"),
    ("powder_recoat", "completion"),
    ("laser_scan", "pause"),
    ("powder_recoat", "pause"),
    ("pause", "laser_scan"),
    ("pause", "powder_recoat"),
    ("preparation", "pause"),
    ("pause", "preparation"),
}


def _get(event: Any, field: str, default=None):
    return event.get(field, default) if isinstance(event, dict) else getattr(event, field, default)


def _canonical_phase(event: Any) -> str | None:
    phase = str(_get(event, "phase", "") or "").lower()
    event_type = str(_get(event, "event_type", "") or "").lower()
    text = f"{phase} {event_type}"
    if any(word in text for word in ("alarm", "error", "fault", "авар")):
        return "alarm"
    if "pause" in text:
        return "pause"
    if any(word in text for word in ("pour", "recoat", "powder", "coater")):
        return "powder_recoat"
    if "burn" in text or "scan" in text or "laser" in text:
        return "laser_scan"
    if any(word in text for word in ("finish", "complete", "shutdown", "print_end")):
        return "completion"
    if any(word in text for word in ("start", "prepare", "purge", "init", "warm")):
        return "preparation"
    return None


def discover_process_model(events: list[Any]) -> dict[str, Any]:
    """Build a directly-follows graph and compare it with a conservative reference model."""
    ordered = sorted(
        enumerate(events),
        key=lambda item: (str(_get(item[1], "ts", "") or ""), item[0]),
    )
    raw_trace = [phase for _, event in ordered if (phase := _canonical_phase(event))]
    trace: list[str] = []
    for phase in raw_trace:
        if not trace or trace[-1] != phase:
            trace.append(phase)
    if len(trace) < 2:
        return {
            "status": "insufficient_data",
            "recognized_events": len(raw_trace),
            "trace_length": len(trace),
        }
    transitions = Counter(zip(trace, trace[1:]))
    unexpected = [
        (edge, count) for edge, count in transitions.items()
        if edge not in _ALLOWED_TRANSITIONS and "alarm" not in edge
    ]
    alarm_edges = [(edge, count) for edge, count in transitions.items() if "alarm" in edge]

    def edge_payload(edge: tuple[str, str], count: int) -> dict[str, Any]:
        source, target = edge
        return {
            "from": source,
            "from_name_ru": _PHASE_NAMES_RU[source],
            "to": target,
            "to_name_ru": _PHASE_NAMES_RU[target],
            "count": count,
        }

    return {
        "status": "ok",
        "mode": "shadow",
        "method": "Directly-Follows Graph with reference conformance",
        "recognized_events": len(raw_trace),
        "trace_length": len(trace),
        "trace": [{"phase": phase, "name_ru": _PHASE_NAMES_RU[phase]} for phase in trace[:100]],
        "transitions": [
            edge_payload(edge, count)
            for edge, count in sorted(transitions.items(), key=lambda item: (-item[1], item[0]))
        ],
        "unexpected_transitions": [edge_payload(edge, count) for edge, count in unexpected],
        "alarm_transitions": [edge_payload(edge, count) for edge, count in alarm_edges],
        "conformance_score": round(1.0 - sum(count for _, count in unexpected) / sum(transitions.values()), 4),
        "limitation_ru": "Эталон переходов консервативный; неизвестные типы событий пока пропускаются",
    }


__all__ = ["discover_process_model"]
