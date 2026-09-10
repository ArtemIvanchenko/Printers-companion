"""Bounded session-level conclusions computed from owner-local raw inputs."""
from collections import Counter

from analytics.log_insights.clocks import burn_windows, pause_intervals
from analytics.log_insights.environment import analyze_environment, default_thresholds, sensor_samples
from analytics.log_insights.timing import restart_layer_comparison, time_accounting
from core.config.settings import get_settings
from core.versioning.provenance import build_provenance


def build_log_insights(files, events, *, thresholds=None):
    settings = get_settings()
    explicit_thresholds = thresholds is not None or bool(settings.log_insights_thresholds)
    thresholds = thresholds if thresholds is not None else (settings.log_insights_thresholds or default_thresholds())
    for rule in thresholds.values():
        from analytics.prediction.timing_validation import finite_number
        if not isinstance(rule, dict) or not finite_number(rule.get("high")):
            raise ValueError("Порог анализа среды должен содержать конечное число high")
    pauses = pause_intervals(events, settings.log_insights_clock_timezone)
    windows = burn_windows(events, pauses, settings.log_insights_clock_timezone)
    diagnostics = Counter()
    environment = analyze_environment(
        sensor_samples(files, thresholds, diagnostics, settings.log_insights_clock_timezone)
        if windows or pauses else iter(()),
        windows, pauses, thresholds, max_gap_s=settings.log_insights_max_gap_seconds,
        stable_s=settings.log_insights_stable_seconds,
        required_signals=list(thresholds) if explicit_thresholds else None,
    )
    return {
        "method_version": "log-insights-1.0.0", "source": "calculated",
        "environment": environment,
        "recovery": {**environment.pop("recovery"), "layer_comparison": restart_layer_comparison(events, windows, pauses)},
        "time_accounting": time_accounting(events, pauses),
        "read_diagnostics": dict(diagnostics),
        "provenance": build_provenance(
            "log_insights", inputs={f.relative_path: f.checksum for f in files},
            config={"thresholds": thresholds, "max_gap_seconds": settings.log_insights_max_gap_seconds,
                    "stable_seconds": settings.log_insights_stable_seconds,
                    "clock_timezone": settings.log_insights_clock_timezone},
            parser_versions={f.parse_result.parser_name: f.parse_result.parser_version for f in files if f.parse_result},
            generated_by=settings.compute_node_id,
        ),
    }
