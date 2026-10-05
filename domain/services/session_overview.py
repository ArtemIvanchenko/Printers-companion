"""Build the compact operator projection of one prepared session analysis."""
from datetime import datetime
from typing import Any

from domain.enums.common import SourceFileFamily
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import SessionClassificationResult
from domain.services.session_telemetry import (
    analysis_telemetry,
    chart_telemetry,
    _count_lines,
    _full_range_sensor_telemetry as _full_range_sensor_telemetry,
)
from analytics.data_quality import assess_session_quality
from analytics.process_health import build_process_health
from domain.services.session_analysis import PreparedSessionAnalysis, prepare_session_analysis, measured_snapshot


def compute_session_spans(
    files: list[IngestedFile],
) -> tuple[tuple[datetime | None, datetime | None], tuple[datetime | None, datetime | None]]:
    """Print and layer-burn spans in one pass over the raw parser facts.

    Monitor100 runs continuously and its clock lacks the main log's midnight
    correction, so it is excluded from both spans. Transition starts contribute
    only to the print span. A burn span needs two distinct usable timestamps;
    layer-bearing events retain their existing role as burn-window evidence.
    These are elapsed diagnostic windows, not normal machine-cycle durations.
    """
    start = end = burn_start = burn_end = None
    for file in files:
        pr = file.parse_result
        if not pr or pr.file_family == SourceFileFamily.monitor100_log:
            continue
        for event in pr.events:
            ts = event.ts
            if ts is None:
                continue
            if start is None or ts < start:
                start = ts
            if end is None or ts > end:
                end = ts
            if (
                event.layer is not None or "burn" in (event.event_type or "").lower()
                or (event.phase or "").lower().strip() == "burn"
            ):
                if burn_start is None or ts < burn_start:
                    burn_start = ts
                if burn_end is None or ts > burn_end:
                    burn_end = ts
        for transition in pr.transitions:
            ts = transition.ts_start
            if ts is not None:
                if start is None or ts < start:
                    start = ts
                if end is None or ts > end:
                    end = ts
    if burn_start is None or burn_end <= burn_start:
        burn_start = burn_end = None
    return (start, end), (burn_start, burn_end)


def _session_machine_seconds(files: list[IngestedFile]) -> float | None:
    """Sum of real per-layer burn+pour seconds from this session's time_log(s).

    This legacy subtotal is NOT the full normal machine cycle and its
    difference from elapsed time is NOT measured idle/pause duration.
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
    analysis: PreparedSessionAnalysis | None = None,
) -> dict[str, Any]:
    """Produce the enriched ``group`` payload the dashboard expects."""
    analysis = analysis or prepare_session_analysis(files, production_context)
    classification = classification or analysis.classification
    events = analysis.events
    raw_features = analysis.features

    total_events = len(events)
    total_lines = sum(_count_lines(f.parse_result) for f in files if f.parse_result)

    # Print timespan (monitor100 excluded — see compute_session_spans). Displayed
    # first/last times follow the same span so the table's times and its
    # duration stay consistent. Fall back to group anchors when unavailable.
    (span_start, span_end), (burn_start, burn_end) = compute_session_spans(files)
    disp_start = span_start or start_ts
    disp_end = span_end or end_ts

    # Count burn events without double-counting (event_type takes priority over phase).
    burn_events = sum(
        1 for e in events
        if "burn" in (e.event_type or "").lower()
        or (e.phase or "").lower().strip() == "burn"
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

    # Preserve the legacy subtotal field, but never call its residual a pause.
    machine_sec = _session_machine_seconds(files)
    residual_sec = max(0.0, duration_sec - machine_sec) if machine_sec is not None and duration_sec > 0 else None

    features = {
        **raw_features,
        "first_time": disp_start.strftime("%H:%M") if disp_start else "-",
        "last_time": disp_end.strftime("%H:%M") if disp_end else "-",
        "duration_sec": duration_sec,
        "duration_min": round(duration_sec / 60, 1),
        "machine_seconds": round(machine_sec, 1) if machine_sec is not None else None,
        "machine_min": round(machine_sec / 60, 1) if machine_sec is not None else None,
        "machine_time_scope": "eligible_measured_burn_plus_pour_only",
        "unattributed_elapsed_seconds": round(residual_sec, 1) if residual_sec is not None else None,
        "idle_seconds": None,
        "idle_min": None,
        "idle_pct": None,
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

    full_telemetry, signal_stats, telemetry_evidence = analysis_telemetry(
        files, burn_start or span_start, burn_end or span_end,
    )
    health = build_process_health(full_telemetry)
    health["evidence"] = telemetry_evidence
    telemetry = chart_telemetry(full_telemetry)
    # Surface the headline readiness score in features for the dashboard cards/table.
    features["atmosphere_readiness"] = (health.get("readiness") or {}).get("score")
    features["process_anomaly_count"] = len(health.get("anomalies", []))

    from analytics.phase_statistics import compute_layer_phase_statistics
    from analytics.soft_sensors import compute_soft_sensors

    soft_sensors = compute_soft_sensors(full_telemetry, signal_stats)
    phase_statistics = compute_layer_phase_statistics(analysis.source_events)
    # Optional enrichment is deliberately outside the required import path.
    advanced_monitoring = {
        "status": "not_requested", "mode": "shadow", "operator_action_allowed": False,
        "successful_algorithms": 0, "algorithms": {},
        "detail_ru": "Теневые методы запускаются отдельно от обязательного анализа.",
    }
    from analytics.log_insights.pipeline import build_log_insights

    log_insights = build_log_insights(files, analysis.source_events)
    accounting = log_insights["time_accounting"]
    features["explicit_pause_seconds"] = accounting["explicit_pause_seconds"]
    features["open_pause_count"] = accounting["open_pause_count"]
    features["normal_machine_cycle_seconds"] = accounting["normal_unique_layer_seconds"]
    features["soft_sensor_count"] = soft_sensors["available"]
    features["phase_statistics_available"] = phase_statistics["available"]
    features["shadow_algorithm_count"] = advanced_monitoring["successful_algorithms"]

    # Data-reliability assessment: trust the inputs before analysing them.
    data_quality = assess_session_quality(files, events, full_telemetry, signal_stats)
    features["data_quality_score"] = data_quality["score"]
    features["data_quality_grade"] = data_quality["grade"]

    overview = {
        "group_id": group_id,
        "classification": classification.classification.value,
        "confidence": round(classification.confidence or grouping_confidence, 2),
        "evidence": classification.evidence,
        "features": features,
        "telemetry": telemetry,
        "telemetry_evidence": telemetry_evidence,
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
    overview["analysis_snapshot"] = measured_snapshot(analysis, overview)
    return overview


def _compute_full_signal_stats(files: list[IngestedFile]) -> dict[str, Any]:
    """Compatibility helper: all available sources, not just the first file."""
    return analysis_telemetry(files)[1]
