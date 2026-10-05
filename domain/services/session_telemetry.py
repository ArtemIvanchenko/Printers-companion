"""Local telemetry inputs and bounded browser projections; no database access."""
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile

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


def analysis_telemetry(
    files: list[IngestedFile],
    active_start: datetime | None = None,
    active_end: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read every available sensor source before any browser downsampling.

    Full arrays are operator-local and never persisted. A missing raw source
    makes coverage partial; the bounded parser table is labelled as a fallback,
    never silently advertised as a complete measurement.
    """
    from collections import Counter
    from zoneinfo import ZoneInfo
    from analytics.log_insights.clocks import seconds
    from analytics.log_insights.environment import sensor_samples
    from analytics.telemetry_parser import _GROUP, compute_signal_stats
    from analytics.thresholds import load_alarm_thresholds
    from core.config.settings import get_settings

    sources = [f for f in files if f.classification.family == SourceFileFamily.sensors_log]
    zone = get_settings().log_insights_clock_timezone
    start_limit, end_limit = seconds(active_start, zone), seconds(active_end, zone)
    columns: dict[str, list] = {}
    timestamps: list[str] = []
    clocks: list[str] = []
    diagnostics = Counter()
    last_timestamp = None
    for timestamp, values in sensor_samples(
        sources, {key: {} for key in _GROUP}, diagnostics, zone,
        apply_profile_ranges=False,
    ):
        if ((start_limit is not None and timestamp < start_limit)
                or (end_limit is not None and timestamp > end_limit)):
            continue
        if last_timestamp is not None and timestamp <= last_timestamp:
            diagnostics["overlapping_or_out_of_order_rows"] += 1
            continue
        last_timestamp = timestamp
        moment = datetime.fromtimestamp(timestamp, ZoneInfo(zone)).replace(tzinfo=None)
        row_index = len(timestamps)
        timestamps.append(moment.isoformat())
        clocks.append(
            f"{moment.day:02d}.{moment.month:02d} "
            f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}"
        )
        for key in values.keys() - columns.keys():
            columns[key] = [None] * row_index
        for key in columns:
            columns[key].append(values.get(key))
    has_raw_sources = any(Path(f.path).is_file() for f in sources)
    if timestamps or has_raw_sources:
        telemetry = _assemble_groups(clocks, columns)
        telemetry["timestamps"] = timestamps
        telemetry["scope"] = "active_print" if active_start and active_end else "full_sensor_session"
        telemetry["clock_timezone"] = zone
        telemetry["timestamp_diagnostics"] = dict(diagnostics)
        stats = compute_signal_stats(columns, load_alarm_thresholds())
        source_kind = "full_available_sensor_stream"
    else:
        telemetry = _sample_telemetry(files, limit=None)
        columns = {key: values for group in telemetry.values() if isinstance(group, dict)
                   for key, values in group.items() if isinstance(values, list)}
        stats = compute_signal_stats(columns, load_alarm_thresholds())
        source_kind = "bounded_parser_tables" if telemetry else "unavailable"
    parsed_files = [f for f in files if getattr(f, "parse_result", None) is not None]
    telemetry["layer_burn_times"] = _layer_burn_times(parsed_files)
    telemetry["layer_time_points"] = _layer_time_points(parsed_files)
    sample_count = len(telemetry.get("time") or [])
    missing = sum(not Path(f.path).is_file() for f in sources)
    evidence = {
        "source": source_kind,
        "sample_count": sample_count,
        "source_file_count": len(sources),
        "missing_file_count": missing,
        "scope": telemetry.get("scope", "bounded_parser_tables"),
        "read_diagnostics": dict(diagnostics),
        "complete_available_stream": bool(timestamps) and not missing
            and not diagnostics.get("overlapping_or_out_of_order_rows")
            and not diagnostics.get("invalid_timestamps"),
        "limitations_ru": [
            "Полный доступный поток не доказывает полноту выгрузки с принтера.",
            "Пропуски и отсутствующие исходники не интерполируются; выборка для графика не используется для диагностики.",
        ],
    }
    return telemetry, stats, evidence


def chart_telemetry(telemetry: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    """A bounded display projection. Changing its budget cannot change analysis."""
    limit = _MAX_TELEMETRY_POINTS if limit is None else max(2, limit)
    count = len(telemetry.get("time") or [])
    picks = list(range(count)) if count <= limit else [round(i * (count - 1) / (limit - 1)) for i in range(limit)]
    result = {}
    for key, value in telemetry.items():
        if key == "layer_time_points":
            continue
        if key in {"time", "timestamps"}:
            result[key] = [value[i] for i in picks]
        elif key in {"oxygen", "temperatures", "gas_temperature", "humidity", "pressure", "diagnostic_pressure"}:
            result[key] = {signal: [values[i] for i in picks] for signal, values in value.items()}
        else:
            result[key] = value
    result["source_sample_count"] = count
    result["display_sample_count"] = len(picks)
    return result


def _full_range_sensor_telemetry(files, active_start=None, active_end=None):
    """Compatibility projection; production prepares full inputs once."""
    telemetry, _, evidence = analysis_telemetry(files, active_start, active_end)
    return chart_telemetry(telemetry) if evidence["source"] == "full_available_sensor_stream" else {}


def _sample_telemetry(files: list[IngestedFile], limit: int | None = _MAX_TELEMETRY_POINTS) -> dict[str, Any]:
    """Fallback chart series from the bounded in-memory table sample."""
    table = _best_telemetry_table(files)
    if table is None:
        return {}
    rows = table.rows if limit is None else _downsample(table.rows, limit)
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
