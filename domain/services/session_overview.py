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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import SessionClassificationResult, classify_session
from analytics.data_quality import assess_session_quality
from analytics.features.extraction import extract_session_features
from analytics.process_health import build_process_health

logger = logging.getLogger(__name__)

# Raw column -> chart series, grouped by physical meaning (see profiles/m350/signals.yaml).
_OXYGEN_COLUMNS = ["SO1", "SO2"]
_TEMPERATURE_COLUMNS = ["ST3", "ST4", "ST5"]
_GAS_TEMPERATURE_COLUMNS = ["ST1 (flow T)", "Flow T"]
_HUMIDITY_COLUMNS = ["ST1 (flow H)", "Flow H"]
_PRESSURE_COLUMNS = ["SP4"]
_DIAGNOSTIC_PRESSURE_COLUMNS = ["SP2", "SP11", "SP12"]
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
    wanted = set(
        _OXYGEN_COLUMNS + _TEMPERATURE_COLUMNS + _GAS_TEMPERATURE_COLUMNS
        + _PRESSURE_COLUMNS + _DIAGNOSTIC_PRESSURE_COLUMNS + _HUMIDITY_COLUMNS
    )
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
                if isinstance(v, (int, float)) and math.isfinite(v) and abs(v) <= 1e7:
                    cleaned.append(v)
                else:
                    cleaned.append(None)
            out[col] = cleaned
    return out


def _assemble_groups(time_axis: list, col_series: dict[str, list]) -> dict[str, Any]:
    """Group a {column: [values]} dict into the chart's semantic groups."""
    def grp(colnames: list[str]) -> dict[str, list]:
        return {c: col_series[c] for c in colnames if c in col_series}
    telemetry: dict[str, Any] = {"time": time_axis}
    if (oxygen := grp(_OXYGEN_COLUMNS)):
        telemetry["oxygen"] = oxygen
    if (temps := grp(_TEMPERATURE_COLUMNS)):
        telemetry["temperatures"] = temps
    if (gas_temps := grp(_GAS_TEMPERATURE_COLUMNS)):
        telemetry["gas_temperature"] = gas_temps
    if (humidity := grp(_HUMIDITY_COLUMNS)):
        telemetry["humidity"] = humidity
    if (pressure := grp(_PRESSURE_COLUMNS)):
        telemetry["pressure"] = pressure
    if (diagnostic_pressure := grp(_DIAGNOSTIC_PRESSURE_COLUMNS)):
        telemetry["diagnostic_pressure"] = diagnostic_pressure
    return telemetry


def _sensor_file_date(file: IngestedFile):
    match = re.search(r"(?<!\d)(\d{2}\.\d{2}\.\d{4})(?!\d)", Path(file.path).name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date()
    except ValueError:
        return None


def _full_range_sensor_telemetry(
    files: list[IngestedFile],
    active_start: datetime | None = None,
    active_end: datetime | None = None,
) -> dict[str, Any]:
    """Chart series sampled across all sensor logs, clipped to active printing.

    The bounded table sample keeps only the first ~5000 rows, so for a long
    print the chart would show only its first ~80 minutes. Reading the full
    file (evenly downsampled) makes the chart span the whole run.
    """
    sensor_files = [
        file for file in files
        if file.classification and file.classification.family == SourceFileFamily.sensors_log
        and Path(file.path).exists()
    ]
    if not sensor_files:
        return {}
    from collections import Counter
    from random import Random
    from zoneinfo import ZoneInfo
    from analytics.log_insights.clocks import seconds
    from analytics.log_insights.environment import sensor_samples
    from core.config.settings import get_settings

    rows: list[dict[str, Any]] = []
    cols = (
        _OXYGEN_COLUMNS + _TEMPERATURE_COLUMNS + _GAS_TEMPERATURE_COLUMNS
        + _HUMIDITY_COLUMNS + _PRESSURE_COLUMNS + _DIAGNOSTIC_PRESSURE_COLUMNS
    )
    zone = get_settings().log_insights_clock_timezone
    start_limit, end_limit = seconds(active_start, zone), seconds(active_end, zone)
    rng, count, first, last = Random(42), 0, None, None
    diagnostics = Counter()
    # Reservoir across the complete, dated stream: a file opened on 18 July
    # can contain 19–21 July too. Preserve both endpoints of the chart.
    for timestamp, values in sensor_samples(sensor_files, {key: {"high": 0} for key in cols}, diagnostics, zone):
        if ((start_limit is not None and timestamp < start_limit)
                or (end_limit is not None and timestamp > end_limit)):
            continue
        moment = datetime.fromtimestamp(timestamp, ZoneInfo(zone)).replace(tzinfo=None)
        row = {**values, "__timestamp": moment.isoformat(), _TIME_COLUMN: moment.strftime("%d.%m %H:%M:%S")}
        if first is None or row["__timestamp"] < first["__timestamp"]:
            first = row
        if last is None or row["__timestamp"] > last["__timestamp"]:
            last = row
        count += 1
        if len(rows) < _MAX_TELEMETRY_POINTS - 2:
            rows.append(row)
        elif (index := rng.randrange(count)) < len(rows):
            rows[index] = row
    if first is not None:
        rows = sorted({row["__timestamp"]: row for row in [first, *rows, last]}.values(),
                      key=lambda row: row["__timestamp"])
    col_series = {
        col: [row.get(col) for row in rows]
        for col in cols
        if any(isinstance(row.get(col), (int, float)) for row in rows)
    }
    if not col_series:
        return {}
    time_axis = [row.get(_TIME_COLUMN) for row in rows]
    result = _assemble_groups(time_axis, col_series)
    timestamps = [row.get("__timestamp") for row in rows]
    if any(value is not None for value in timestamps):
        result["timestamps"] = timestamps
    result["scope"] = "active_print" if active_start and active_end else "full_sensor_session"
    result["clock_timezone"] = zone
    result["timestamp_diagnostics"] = dict(diagnostics)
    return result


def _sample_telemetry(files: list[IngestedFile]) -> dict[str, Any]:
    """Fallback chart series from the bounded in-memory table sample."""
    table = _best_telemetry_table(files)
    if table is None:
        return {}
    rows = _downsample(table.rows, _MAX_TELEMETRY_POINTS)
    has_time = any(_TIME_COLUMN in r for r in rows[:20])
    time_axis = [r.get(_TIME_COLUMN) for r in rows] if has_time else list(range(len(rows)))
    col_series: dict[str, list] = {}
    for col_list in (
        _OXYGEN_COLUMNS,
        _TEMPERATURE_COLUMNS,
        _GAS_TEMPERATURE_COLUMNS,
        _HUMIDITY_COLUMNS,
        _PRESSURE_COLUMNS,
        _DIAGNOSTIC_PRESSURE_COLUMNS,
    ):
        col_series.update(_series(rows, col_list))
    return _assemble_groups(time_axis, col_series)


def _build_telemetry(
    files: list[IngestedFile],
    active_start: datetime | None = None,
    active_end: datetime | None = None,
) -> dict[str, Any]:
    # Prefer the full-range series (whole sensors.log); fall back to the bounded
    # table sample when the raw file isn't on disk (e.g. re-analysed payloads).
    telemetry = _full_range_sensor_telemetry(files, active_start, active_end) or _sample_telemetry(files)
    telemetry["layer_burn_times"] = _layer_burn_times(files)
    # Temporary correlation index: health anomalies consume it immediately
    # and store only their matched layer/range. It is removed before the group
    # payload is persisted, avoiding thousands of duplicate timestamp rows.
    telemetry["layer_time_points"] = _layer_time_points(files)
    return telemetry


def _layer_burn_times(files: list[IngestedFile]) -> list[dict[str, Any]]:
    """Per-layer burn duration.

    Preferred source: the time.log ``OLD_STATS`` lines, parsed into
    ``layer_timing_summary`` events whose payload carries ``burn_ms`` directly
    (the machine's own per-layer burn duration). Second choice: derive it from
    the ``NEW_STATS`` ``Burn_Start``/``Burn_End`` absolute-ms counters. Last
    resort: approximate from a burn table's N + Time columns.

    (Note: payloads are structured dicts — there is no ``raw_text`` field to
    regex; reading the parsed numbers directly is both correct and cheaper.)
    """
    from analytics.prediction.scan_calibration import _burn_seconds_by_layer

    timing_events = [e for f in files if f.parse_result
                     and f.parse_result.file_family == SourceFileFamily.time_log
                     for e in f.parse_result.events]
    if any(e.event_type == "layer_timing_summary" for e in timing_events):
        return [{"layer": layer, "duration_sec": round(value, 1)}
                for layer, value in sorted(_burn_seconds_by_layer(timing_events).items())]
    seen: dict[int, float] = {}
    # NEW_STATS fallback accumulators (used only if OLD_STATS is absent).
    burn_start: dict[int, int] = {}
    burn_end: dict[int, int] = {}

    for file in files:
        pr = file.parse_result
        if not pr or pr.file_family != SourceFileFamily.time_log:
            continue
        for event in pr.events:
            payload = event.payload or {}
            layer = payload.get("layer")
            if not isinstance(layer, int):
                continue
            if event.event_type == "layer_timing_summary":
                burn_ms = payload.get("burn_ms")
                if isinstance(burn_ms, int) and burn_ms > 0 and layer not in seen:
                    seen[layer] = round(burn_ms / 1000.0, 1)
            elif event.event_type == "burn_start" and isinstance(payload.get("abs_ms"), int):
                burn_start.setdefault(layer, payload["abs_ms"])
            elif event.event_type == "burn_end" and isinstance(payload.get("abs_ms"), int):
                burn_end[layer] = payload["abs_ms"]

    # If no OLD_STATS summaries, derive from NEW_STATS start/end abs-ms counters.
    if not seen:
        for layer in burn_start.keys() & burn_end.keys():
            dur_ms = burn_end[layer] - burn_start[layer]
            if dur_ms > 0:
                seen[layer] = round(dur_ms / 1000.0, 1)

    if seen:
        return [{"layer": layer, "duration_sec": dur} for layer, dur in sorted(seen.items())]

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
    return result


def _layer_time_points(files: list[IngestedFile]) -> list[dict[str, Any]]:
    """Physical layer timestamps from daily burn logs for sensor correlation."""
    points: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for file in files:
        if (
            not file.classification
            or file.classification.family != SourceFileFamily.burn_log
            or not file.parse_result
        ):
            continue
        file_date = _sensor_file_date(file)
        if file_date is None:
            continue
        for table in file.parse_result.tables:
            for row in table.rows:
                layer = row.get(_LAYER_COLUMN)
                seconds = _clock_to_seconds(row.get(_TIME_COLUMN))
                if not isinstance(layer, int) or seconds is None:
                    continue
                timestamp = (
                    datetime.combine(file_date, datetime.min.time())
                    + timedelta(seconds=seconds)
                ).isoformat()
                key = (layer, timestamp)
                if key not in seen:
                    points.append({"layer": layer, "timestamp": timestamp})
                    seen.add(key)
    points.sort(key=lambda point: point["timestamp"])
    return points


def compute_burn_span(
    files: list[IngestedFile],
) -> tuple[datetime | None, datetime | None]:
    """First/last timestamped layer burn, excluding purge and shutdown phases."""
    burn_ts: list[datetime] = []
    for file in files:
        parse_result = file.parse_result
        if not parse_result or parse_result.file_family == SourceFileFamily.monitor100_log:
            continue
        for event in parse_result.events:
            event_type = (event.event_type or "").lower()
            phase = (event.phase or "").lower().strip()
            if event.ts is not None and (
                phase == "burn" or "burn" in event_type or event.layer is not None
            ):
                burn_ts.append(event.ts)
    if len(burn_ts) < 2 or max(burn_ts) <= min(burn_ts):
        return None, None
    return min(burn_ts), max(burn_ts)


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


def _session_machine_seconds(files: list[IngestedFile]) -> float | None:
    """Sum of real per-layer burn+pour seconds from this session's time_log(s).

    None when no time_log is present. Deliberately does not apply the coverage
    floor used by ``accuracy._machine_hours_from_logs`` (that guards against a
    partial log masquerading as a whole print's calibration input) — here it
    is only ever compared against the wall-clock span of these same files, so
    a partial log still yields an honest (smaller) idle-time reading for the
    time window it actually covers.
    """
    from analytics.prediction.recoat_calibration import machine_seconds_from_events

    by_layer = machine_seconds_from_events([
        e for f in files if f.classification.family == SourceFileFamily.time_log and f.parse_result
        for e in f.parse_result.events
    ])
    return sum(by_layer.values()) if by_layer else None


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
    first_layer = min(layer_nums) if layer_nums else None
    last_layer = max(layer_nums) if layer_nums else None

    # Duration: prefer the monitor100-excluded print span; fall back to group
    # anchors. (raw_features["duration_sec"] spans ALL events incl. monitor100,
    # so it is NOT used here — it would reintroduce the inflation.)
    duration_sec: float | None = None
    if isinstance(span_start, datetime) and isinstance(span_end, datetime) and span_end > span_start:
        duration_sec = (span_end - span_start).total_seconds()
    elif isinstance(start_ts, datetime) and isinstance(end_ts, datetime) and end_ts > start_ts:
        duration_sec = (end_ts - start_ts).total_seconds()
    duration_sec = duration_sec or 0.0

    # Pause-free machine time (burn+pour per layer, from time_log) vs. the
    # wall-clock span above — the gap is idle/pause time the geometry-based
    # prediction never claims to cover (see AGENT_NOTES.md, "многодневные
    # печати" — a real build showed ~18h idle out of ~47.6h wall time). Shown
    # here so the operator sees it explicitly instead of it being silently
    # absorbed into "duration". None when this session has no time_log.
    machine_sec = _session_machine_seconds(files)
    idle_sec = max(0.0, duration_sec - machine_sec) if machine_sec is not None and duration_sec > 0 else None

    features = {
        **raw_features,
        "first_time": disp_start.strftime("%H:%M") if disp_start else "-",
        "last_time": disp_end.strftime("%H:%M") if disp_end else "-",
        "duration_sec": duration_sec,
        "duration_min": round(duration_sec / 60, 1),
        "machine_seconds": round(machine_sec, 1) if machine_sec is not None else None,
        "machine_min": round(machine_sec / 60, 1) if machine_sec is not None else None,
        "idle_seconds": round(idle_sec, 1) if idle_sec is not None else None,
        "idle_min": round(idle_sec / 60, 1) if idle_sec is not None else None,
        "idle_pct": round(idle_sec / duration_sec * 100, 1) if idle_sec is not None and duration_sec > 0 else None,
        "total_lines": total_lines,
        "total_events": total_events,
        "layers": layers,
        "first_layer": first_layer,
        "last_layer": last_layer,
        "burn_events": burn_events,
        "file_count": len(files),
        "pause_count": raw_features.get("pause_count", 0),
        "material": raw_features.get("material") or "unknown",
    }

    burn_start, burn_end = compute_burn_span(files)
    telemetry = _build_telemetry(files, burn_start or span_start, burn_end or span_end)
    health = build_process_health(telemetry)
    telemetry.pop("layer_time_points", None)
    # Surface the headline readiness score in features for the dashboard cards/table.
    features["atmosphere_readiness"] = (health.get("readiness") or {}).get("score")
    features["process_anomaly_count"] = len(health.get("anomalies", []))

    # Full-resolution signal stats from the complete sensors.log (all rows, not just 150).
    signal_stats = _compute_full_signal_stats(files)
    from analytics.phase_statistics import compute_layer_phase_statistics
    from analytics.soft_sensors import compute_soft_sensors

    soft_sensors = compute_soft_sensors(telemetry, signal_stats)
    phase_statistics = compute_layer_phase_statistics(events)
    from analytics.process_monitoring import build_advanced_monitoring

    advanced_monitoring = build_advanced_monitoring(telemetry, events)
    from analytics.log_insights.pipeline import build_log_insights

    log_insights = build_log_insights(files, events)
    features["soft_sensor_count"] = soft_sensors["available"]
    features["phase_statistics_available"] = phase_statistics["available"]
    features["shadow_algorithm_count"] = advanced_monitoring["successful_algorithms"]

    # Data-reliability assessment: trust the inputs before analysing them.
    data_quality = assess_session_quality(files, events, telemetry, signal_stats)
    features["data_quality_score"] = data_quality["score"]
    features["data_quality_grade"] = data_quality["grade"]

    return {
        "group_id": group_id,
        "classification": classification.classification.value,
        "confidence": round(classification.confidence or grouping_confidence, 2),
        "evidence": classification.evidence,
        "features": features,
        "telemetry": telemetry,
        "health": health,
        "signal_stats": signal_stats,
        "soft_sensors": soft_sensors,
        "phase_statistics": phase_statistics,
        "advanced_monitoring": advanced_monitoring,
        "log_insights": log_insights,
        "data_quality": data_quality,
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
    """Read alarm_high / alarm_low from signals.yaml (shared loader)."""
    from analytics.thresholds import load_alarm_thresholds
    return load_alarm_thresholds()
