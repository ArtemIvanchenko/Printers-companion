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
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import SessionClassificationResult, classify_session
from analytics.features.extraction import extract_session_features
from analytics.process_health import build_process_health

logger = logging.getLogger(__name__)

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
    # Clamp to valid index range to avoid off-by-one on the last element
    return [rows[min(int(i * step), len(rows) - 1)] for i in range(limit)]


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
            # Collect columns across ALL rows — first row may be missing some
            columns: set[str] = set()
            for row in table.rows[:10]:   # sample up to 10 rows to find all keys
                columns.update(row.keys())
            score = len(wanted & columns)
            if score > best_score:
                best, best_score = table, score
    return best


def _series(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, list]:
    """Extract numeric series for each column present in the table.

    Checks column presence across ALL rows (not just the first row),
    so data is not silently dropped when the first row is missing a key.
    Non-finite values (NaN, Inf) are replaced with None for JSON safety.
    """
    import math
    if not rows:
        return {}
    # Build a set of all column names seen across the table
    all_keys: set[str] = set()
    for row in rows[:20]:   # sample head to find all keys cheaply
        all_keys.update(row.keys())
    out: dict[str, list] = {}
    for col in columns:
        if col not in all_keys:
            continue
        values = [r.get(col) for r in rows]
        if any(isinstance(v, (int, float)) for v in values):
            cleaned = []
            for v in values:
                if isinstance(v, (int, float)) and math.isfinite(v):
                    cleaned.append(v)
                else:
                    cleaned.append(None)
            out[col] = cleaned
    return out


def _build_telemetry(files: list[IngestedFile]) -> dict[str, Any]:
    table = _best_telemetry_table(files)
    if table is None:
        return {}
    rows = _downsample(table.rows, _MAX_TELEMETRY_POINTS)
    # Probe for the Time column across sampled rows, not just rows[0] — the first
    # row may be missing it even when the rest of the table carries timestamps.
    has_time = any(_TIME_COLUMN in r for r in rows[:20])
    time_axis = [r.get(_TIME_COLUMN) for r in rows] if has_time else list(range(len(rows)))

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
            if not t.rows:
                continue
            # Sample several rows: the first may omit columns the table carries.
            keys: set[str] = set()
            for row in t.rows[:10]:
                keys.update(row.keys())
            if _LAYER_COLUMN in keys and _TIME_COLUMN in keys:
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


def compute_print_span(
    files: list[IngestedFile],
) -> tuple[datetime | None, datetime | None]:
    """Start/end of the actual print, derived from event timestamps.

    Excludes the monitor100 daemon log: it runs continuously (not just during
    the print), so its early-morning timestamps would inflate the span, and it
    does not apply the midnight-rollover (day_shift) correction the main event
    log does — making its absolute times unreliable for measuring duration.

    Returns (None, None) when no usable timestamps are present.
    """
    print_ts: list[datetime] = []
    for f in files:
        pr = f.parse_result
        if not pr or pr.file_family == SourceFileFamily.monitor100_log:
            continue
        print_ts.extend(e.ts for e in pr.events if e.ts is not None)
        print_ts.extend(t.ts_start for t in pr.transitions if t.ts_start is not None)
    if not print_ts:
        return None, None
    return min(print_ts), max(print_ts)


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

    # Print timespan (monitor100 excluded — see compute_print_span). Displayed
    # first/last times follow the same span so the table's times and its
    # duration stay consistent. Fall back to group anchors when unavailable.
    span_start, span_end = compute_print_span(files)
    disp_start = span_start or start_ts
    disp_end = span_end or end_ts

    # Count burn events without double-counting (event_type takes priority over phase).
    burn_events = sum(
        1 for e in events
        if "burn" in (e.event_type or "").lower()
        or (e.phase or "").lower().strip() == "burn"
        and "burn" not in (e.event_type or "").lower()
    )

    # Layer count: number of unique printed layers in this session.
    layer_nums = {e.layer for e in events if e.layer is not None}
    layers = len(layer_nums) if layer_nums else 0

    # Duration: prefer the monitor100-excluded print span; fall back to group
    # anchors. (raw_features["duration_sec"] spans ALL events incl. monitor100,
    # so it is NOT used here — it would reintroduce the inflation.)
    duration_sec: float | None = None
    if isinstance(span_start, datetime) and isinstance(span_end, datetime) and span_end > span_start:
        duration_sec = (span_end - span_start).total_seconds()
    elif isinstance(start_ts, datetime) and isinstance(end_ts, datetime) and end_ts > start_ts:
        duration_sec = (end_ts - start_ts).total_seconds()
    duration_sec = duration_sec or 0.0

    features = {
        **raw_features,
        "first_time": disp_start.strftime("%H:%M") if disp_start else "-",
        "last_time": disp_end.strftime("%H:%M") if disp_end else "-",
        "duration_sec": duration_sec,
        "duration_min": round(duration_sec / 60, 1),
        "total_lines": total_lines,
        "total_events": total_events,
        "layers": layers,
        "burn_events": burn_events,
        "file_count": len(files),
        "pause_count": raw_features.get("pause_count", 0),
        "material": raw_features.get("material") or "unknown",
    }

    telemetry = _build_telemetry(files)
    health = build_process_health(telemetry)
    # Surface the headline readiness score in features for the dashboard cards/table.
    features["atmosphere_readiness"] = (health.get("readiness") or {}).get("score")
    features["process_anomaly_count"] = len(health.get("anomalies", []))

    # Full-resolution signal stats from the complete sensors.log (all rows, not just 150).
    signal_stats = _compute_full_signal_stats(files)

    return {
        "group_id": group_id,
        "classification": classification.classification.value,
        "confidence": round(classification.confidence or grouping_confidence, 2),
        "evidence": classification.evidence,
        "features": features,
        "telemetry": telemetry,
        "health": health,
        "signal_stats": signal_stats,
        # Timestamps preserved in payload so save_session_payload can populate
        # BuildSession.start_ts / end_ts (dashboard ordering + charts). Use the
        # print span (monitor100 excluded) when available so the persisted times
        # match the displayed first_time/last_time; fall back to group anchors.
        "start_ts": disp_start.isoformat() if disp_start else None,
        "end_ts": disp_end.isoformat() if disp_end else None,
    }


def _compute_full_signal_stats(files: list[IngestedFile]) -> dict[str, Any]:
    """Parse the raw sensors.log with Polars and compute full-resolution stats.

    Finds the *_sensors.log file in the session group, reads all rows (~330k),
    and returns per-signal statistics (mean, std, p95, p99, alarm_count, etc.).
    Falls back to an empty dict if no sensors file is present or parsing fails.
    """
    sensors_file: IngestedFile | None = None
    for f in files:
        if f.classification and f.classification.family == SourceFileFamily.sensors_log:
            sensors_file = f
            break

    if sensors_file is None:
        return {}

    path = Path(sensors_file.path)
    if not path.exists():
        logger.warning("Sensors log not found at %s — skipping full stats", path)
        return {}

    try:
        from analytics.telemetry_parser import compute_full_signal_stats
        # Load alarm thresholds from signals.yaml for alarm_count computation.
        alarm_thresholds = _load_alarm_thresholds()
        stats = compute_full_signal_stats(path, alarm_thresholds=alarm_thresholds)
        logger.info("Full signal stats computed from %s (%d signals)", path.name, len(stats))
        return stats
    except Exception as exc:
        logger.warning("Full signal stats failed for %s: %s", path.name, exc)
        return {}


def _load_alarm_thresholds() -> dict[str, dict[str, float]]:
    """Read alarm_high / alarm_low from signals.yaml for alarm_count tracking."""
    try:
        from profiles.base.profile import load_yaml
        signals_path = Path(__file__).resolve().parents[2] / "profiles" / "m350" / "signals.yaml"
        raw = load_yaml(signals_path)
        result: dict[str, dict[str, float]] = {}
        for sig_name, sig_data in (raw.get("signals") or {}).items():
            entry: dict[str, float] = {}
            if (ah := sig_data.get("alarm_high")) is not None:
                entry["alarm_high"] = float(ah)
            if (al := sig_data.get("alarm_low")) is not None:
                entry["alarm_low"] = float(al)
            if entry:
                result[sig_name] = entry
        return result
    except Exception:
        return {}
