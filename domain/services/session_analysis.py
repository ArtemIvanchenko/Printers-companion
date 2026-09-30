"""One prepared event interpretation shared by every session projection.

This is operator-local orchestration over detached parser DTOs. No SQL, object
store or HTTP dependency. Raw timing attempts remain untouched: semantic event
deduplication is not a substitute for the dedicated timing admission rules.
"""
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from analytics.features.extraction import extract_layer_features, extract_session_features
from analytics.normalization.deduplication import deduplicate_events
from analytics.segmentation.phase_segmenter import segment_phases
from core.config.settings import get_settings
from core.versioning.provenance import build_provenance
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import classify_session
from profiles.base.profile import PrinterProfilePlugin


@dataclass
class PreparedSessionAnalysis:
    analysis_id: str
    events: list
    source_events: list
    transitions: list
    dedupe_diagnostics: list
    classification: Any
    features: dict
    layer_features: list
    phase_segments: list
    provenance: dict
    profile: PrinterProfilePlugin


def prepare_session_analysis(
    files: list[IngestedFile],
    production_context: dict | None = None,
    *,
    profile: PrinterProfilePlugin | None = None,
) -> PreparedSessionAnalysis:
    if profile is None:
        # Legacy API callers default to the installed machine; production
        # ingestion passes its explicit selected profile through this boundary.
        from profiles.m350.profile import get_profile
        profile = get_profile()
    source_events = [e for f in files if f.parse_result for e in f.parse_result.events]
    transitions = [t for f in files if f.parse_result for t in f.parse_result.transitions]
    events, diagnostics = deduplicate_events(source_events)
    settings = get_settings()
    provenance = build_provenance(
        "session_analysis",
        inputs={f.relative_path: f.checksum for f in files},
        config={"profile_id": profile.profile_id, "profile_version": profile.version,
                "signals": profile.signal_mappings, "phase_rules": profile.phase_rules,
                "source_policy": {"excluded": profile.excluded_source_patterns,
                                  "size_limits": profile.source_size_limits},
                "context": production_context or {},
                "clock_timezone": settings.log_insights_clock_timezone,
                "thresholds": settings.log_insights_thresholds,
                "max_gap_seconds": settings.log_insights_max_gap_seconds,
                "stable_seconds": settings.log_insights_stable_seconds},
        parser_versions={f.parse_result.parser_name: f.parse_result.parser_version
                         for f in files if f.parse_result},
        generated_by=settings.compute_node_id,
    )
    return PreparedSessionAnalysis(
        analysis_id=f"analysis_{uuid4().hex}", events=events, source_events=source_events,
        transitions=transitions, dedupe_diagnostics=diagnostics,
        classification=classify_session(files),
        features=extract_session_features(events, transitions, production_context),
        layer_features=extract_layer_features(events),
        phase_segments=segment_phases(events, transitions, profile.phase_rules),
        provenance=provenance, profile=profile,
    )


def measured_snapshot(analysis: PreparedSessionAnalysis, overview: dict) -> dict:
    """Compact common contract; charts and full event streams are not duplicated."""
    from copy import deepcopy
    return deepcopy({
        "schema_version": 1, "analysis_id": analysis.analysis_id,
        "source": "calculated", "provenance": analysis.provenance,
        "classification": overview["classification"],
        "confidence": overview["confidence"], "evidence": overview["evidence"],
        "features": overview["features"], "health": overview["health"],
        "data_quality": overview["data_quality"], "signal_stats": overview["signal_stats"],
        "time_accounting": overview["log_insights"]["time_accounting"],
        "telemetry_evidence": overview["telemetry_evidence"],
    })
