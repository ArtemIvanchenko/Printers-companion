"""Build a dashboard-ready overview payload for a grouped session.

This bridges the gap between session *grouping* (which only knows which files
belong together) and what the web dashboard reads from
``BuildSession.context.runtime_payload.group``: a classification, a flat set of
display features, and a compact process-telemetry series (oxygen, temperatures,
pressure, humidity, per-layer burn time) decoded via the M-450-M signal dictionary.

Kept intentionally light (single pass over parsed output, no event deduplication)
so it is cheap enough to run inline during API ingest. Storage-agnostic — the
result is a plain JSON-serializable dict persisted by the runtime repository.
"""
import re
from datetime import datetime
from typing import Any

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import SessionClassificationResult, classify_session
from analytics.features.extraction import extract_session_features
from analytics.process_health import build_process_health

# Raw column -> chart series, grouped by physical meaning (see profiles/m350/signals.yaml).
_OXYGEN_COLUMNS = ["SO1", "SO2"]
_TEMPERATURE_COLUMNS = ["ST3", "ST4", "ST5"]
_HUMIDITY_COLUMNS = ["ST1 (flow H)", "Flow H"]
_PRESSURE_COLUMNS = ["SP4"]
_LAYER_COLUMN = "N"
_TIME_COLUMN = "Time"

_MAX_TELEMETRY_POINTS = 150
_LINE_KEYS = ("line_count", "entry_count", "total_rows", "row_count")


def _count_lines(parse_result) -> int:
    md = parse_result.metadata or {}
    for key in _LINE_KEYS:
        value = md.get(key)
        if isinstance(value, int):
            return value
    return 0


def _clock_to_seconds(value: Any) -> float | None:
    """Parse an 'HH:MM:SS(.fff)' clock string into seconds since midnight."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2].replace(",", "."))
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _downsample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


def _best_telemetry_table(files: list[IngestedFile]):
    """Pick the parsed table richest in known sensor columns (burn/sensors logs)."""
    wanted = set(_OXYGEN_COLUMNS + _TEMPERATURE_COLUMNS + _PRESSURE_COLUMNS + _HUMIDITY_COLUMNS)
    best = None
    best_score = 0
    for file in files:
        if not file.parse_result:
            continue
        for table in file.parse_result.tables:
            if not table.rows:
                continue
            columns = set(table.rows[0].keys())
            score = len(wanted & columns)
            if score > best_score:
                best, best_score = table, score
    return best


def _series(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, list]:
    """Extract the first present column from `columns` as a numeric series."""
    out: dict[str, list] = {}
    for col in columns:
        if col in rows[0]:
            values = [r.get(col) for r in rows]
            if any(isinstance(v, (int, float)) for v in values):
                out[col] = [v if isinstance(v, (int, float)) else None for v in values]
    return out


def _build_telemetry(files: list[IngestedFile]) -> dict[str, Any]:
    table = _best_telemetry_table(files)
    if table is None:
        return {}
    rows = _downsample(table.rows, _MAX_TELEMETRY_POINTS)
    time_axis = [r.get(_TIME_COLUMN) for r in rows] if _TIME_COLUMN in rows[0] else list(range(len(rows)))

    telemetry: dict[str, Any] = {"time": time_axis}
    oxygen = _series(rows, _OXYGEN_COLUMNS)
    temps = _series(rows, _TEMPERATURE_COLUMNS)
    humidity = _series(rows, _HUMIDITY_COLUMNS)
    pressure = _series(rows, _PRESSURE_COLUMNS)
    if oxygen:
        telemetry["oxygen"] = oxygen
    if temps:
        telemetry["temperatures"] = temps
    if humidity:
        telemetry["humidity"] = humidity
    if pressure:
        telemetry["pressure"] = pressure
    telemetry["layer_burn_times"] = _layer_burn_times(files)
    return telemetry


_TIMELOG_LAYER = re.compile(r"L(\d+)_detailed")
_TIMELOG_BURN_START = re.compile(r"Burn_Start:(\d+)")
_TIMELOG_BURN_END = re.compile(r"Burn_End:(\d+)")


def _layer_burn_times(files: list[IngestedFile]) -> list[dict[str, Any]]:
    """Per-layer burn duration.

    Preferred source: the time.log ``NEW_STATS`` lines, which carry explicit
    ``Burn_Start``/``Burn_End`` counters per layer (millisecond ticks) — the
    accurate signal. Falls back to approximating from the burn table's N + Time
    columns when time.log is unavailable.
    """
    seen: dict[int, float] = {}
    for file in files:
        pr = file.parse_result
        if not pr or pr.file_family != SourceFileFamily.time_log:
            continue
        for event in pr.events:
            raw = (event.payload or {}).get("raw_text", "")
            m_layer = _TIMELOG_LAYER.search(raw)
            m_start = _TIMELOG_BURN_START.search(raw)
            m_end = _TIMELOG_BURN_END.search(raw)
            if not (m_layer and m_start and m_end):
                continue
            layer = int(m_layer.group(1))
            duration_ms = int(m_end.group(1)) - int(m_start.group(1))
            if duration_ms > 0 and layer not in seen:
                seen[layer] = round(duration_ms / 1000.0, 1)  # ms ticks -> seconds
    if seen:
        return [{"layer": layer, "duration_sec": dur} for layer, dur in sorted(seen.items())][:1000]

    # Fallback: approximate from a burn table's layer (N) + Time columns.
    table = None
    for file in files:
        if not file.parse_result:
            continue
        for t in file.parse_result.tables:
            if t.rows and _LAYER_COLUMN in t.rows[0] and _TIME_COLUMN in t.rows[0]:
                table = t
                break
        if table:
            break
    if table is None:
        return []

    spans: dict[int, list[float]] = {}
    for row in table.rows:
        layer = row.get(_LAYER_COLUMN)
        secs = _clock_to_seconds(row.get(_TIME_COLUMN))
        if not isinstance(layer, int) or secs is None:
            continue
        spans.setdefault(layer, [secs, secs])
        if secs < spans[layer][0]:
            spans[layer][0] = secs
        if secs > spans[layer][1]:
            spans[layer][1] = secs
    result = [
        {"layer": layer, "duration_sec": round(hi - lo, 1)}
        for layer, (lo, hi) in sorted(spans.items())
        if hi >= lo
    ]
    return result[:300]


def build_group_overview(
    group_id: str,
    files: list[IngestedFile],
    *,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    grouping_confidence: float = 0.0,
    production_context: dict[str, Any] | None = None,
    classification: SessionClassificationResult | None = None,
) -> dict[str, Any]:
    """Produce the enriched ``group`` payload the dashboard expects."""
    classification = classification or classify_session(files)

    events = [e for f in files if f.parse_result for e in f.parse_result.events]
    transitions = [t for f in files if f.parse_result for t in f.parse_result.transitions]
    raw_features = extract_session_features(events, transitions, production_context)

    total_events = len(events)
    total_lines = sum(_count_lines(f.parse_result) for f in files if f.parse_result)
    burn_events = sum(
        1 for e in events if "burn" in (e.event_type or "").lower() or e.phase == "burn"
    )
    layers = max((e.layer for e in events if e.layer is not None), default=0)

    duration_sec = raw_features.get("duration_sec") or 0.0
    features = {
        "first_time": start_ts.strftime("%H:%M") if start_ts else "-",
        "last_time": end_ts.strftime("%H:%M") if end_ts else "-",
        "duration_min": round(duration_sec / 60, 1),
        "total_lines": total_lines,
        "total_events": total_events,
        "layers": layers,
        "burn_events": burn_events,
        "file_count": len(files),
        "pause_count": raw_features.get("pause_count", 0),
        "material": raw_features.get("material") or "unknown",
        **raw_features,
    }

    telemetry = _build_telemetry(files)
    health = build_process_health(telemetry)
    # Surface the headline readiness score in features for the dashboard cards/table.
    features["atmosphere_readiness"] = (health.get("readiness") or {}).get("score")
    features["process_anomaly_count"] = len(health.get("anomalies", []))

    return {
        "group_id": group_id,
        "classification": classification.classification.value,
        "confidence": round(classification.confidence or grouping_confidence, 2),
        "evidence": classification.evidence,
        "features": features,
        "telemetry": telemetry,
        "health": health,
    }
