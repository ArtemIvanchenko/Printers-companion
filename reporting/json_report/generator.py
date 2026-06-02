from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.utils.timekeys import ts_sort_key

# PostgreSQL jsonb hard limit is 1 GB.  A stateFlow.log can produce millions
# of events; serialising them all blows the limit.  We keep a capped,
# representative sample: head + important events + sampled tail.
_MAX_TIMELINE_EVENTS = 2_000


def _truncate_timeline(events: list) -> list[dict]:
    """Return at most _MAX_TIMELINE_EVENTS events, preserving structure.

    Strategy:
    - Always keep the first 200 events (startup sequence).
    - Always keep events tagged as errors or alarms (regardless of position).
    - Fill the remaining budget with an evenly-spaced sample of the rest.
    - Append a sentinel at the end when truncated so consumers know.
    """
    if len(events) <= _MAX_TIMELINE_EVENTS:
        return [e.model_dump(mode="json") for e in events]

    PRIORITY_TYPES = {"error", "alarm", "finish", "abort", "restart_attempt", "pause", "resume"}
    head = events[:200]
    rest = events[200:]

    priority = [e for e in rest if getattr(e, "event_type", "") in PRIORITY_TYPES]
    others   = [e for e in rest if getattr(e, "event_type", "") not in PRIORITY_TYPES]

    budget = _MAX_TIMELINE_EVENTS - len(head) - len(priority)
    if budget > 0 and others:
        step = max(1, len(others) // budget)
        sampled = others[::step][:budget]
    else:
        sampled = []

    combined = head + priority + sampled
    combined.sort(key=lambda e: ts_sort_key(getattr(e, "ts", None)))

    result = [e.model_dump(mode="json") for e in combined]
    result.append({
        "event_type": "_truncated",
        "note": (
            f"Timeline truncated: {len(events)} total events → "
            f"{len(result) - 1} kept (head + priority + sample). "
            f"Full data in sensors/stateFlow logs."
        ),
    })
    return result

from analytics.features.extraction import extract_layer_features, extract_session_features
from analytics.normalization.deduplication import deduplicate_events
from analytics.segmentation.phase_segmenter import segment_phases
from core.versioning.constants import (
    ANALYSIS_VERSION,
    CAUSAL_MODEL_VERSION,
    RULE_PACK_VERSION,
    SIGNAL_DICTIONARY_VERSION,
)
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import classify_session
from profiles.m350.profile import get_profile


def generate_session_json_report(
    session_id: str,
    files: list[IngestedFile],
    production_context: dict[str, Any] | None = None,
    quality_outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    profile = get_profile()
    events = [event for file in files if file.parse_result for event in file.parse_result.events]
    transitions = [transition for file in files if file.parse_result for transition in file.parse_result.transitions]
    deduped_events, dedupe_diagnostics = deduplicate_events(events)
    classification = classify_session(files)
    layer_features = extract_layer_features(deduped_events)
    session_features = extract_session_features(deduped_events, transitions, production_context)
    segments = segment_phases(deduped_events, transitions, profile.phase_rules)
    parser_versions = {
        file.parse_result.parser_name: file.parse_result.parser_version
        for file in files
        if file.parse_result
    }
    input_hashes = {file.relative_path: file.checksum for file in files}
    data_quality = summarize_data_quality(files, dedupe_diagnostics)
    return {
        "report_id": f"report_{uuid4().hex}",
        "session_id": session_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_summary": {
            "classification": classification.classification.value,
            "classification_confidence": classification.confidence,
            "classification_evidence": classification.evidence,
            "features": session_features,
        },
        "file_inventory": [
            {
                "path": file.relative_path,
                "family": file.classification.family.value,
                "role": file.classification.role.value,
                "checksum": file.checksum,
                "size_bytes": file.size_bytes,
                "encoding": file.encoding,
                "data_quality_status": file.data_quality_status.value,
            }
            for file in files
        ],
        "data_quality": data_quality,
        "timeline": _truncate_timeline(deduped_events),
        "phase_segments": [segment.model_dump(mode="json") for segment in segments],
        "layer_features": layer_features,
        "anomalies": [],
        "hypotheses": [],
        "operator_context": production_context or {},
        "quality_outcomes": quality_outcomes or [],
        "missing_data": data_quality.get("missing_context", []),
        "known_unknowns": [
            {
                "file": file.relative_path,
                "unknown_columns": table.unknown_columns,
            }
            for file in files
            if file.parse_result
            for table in file.parse_result.tables
            if table.unknown_columns
        ],
        "deduplication_diagnostics": dedupe_diagnostics,
        "version_metadata": {
            "input_file_hashes": input_hashes,
            "parser_versions": parser_versions,
            "profile_version": profile.version,
            "signal_dictionary_version": SIGNAL_DICTIONARY_VERSION,
            "rule_pack_version": RULE_PACK_VERSION,
            "analysis_version": ANALYSIS_VERSION,
            "causal_model_version": CAUSAL_MODEL_VERSION,
            "generated_by": "system",
        },
    }


def summarize_data_quality(files: list[IngestedFile], dedupe_diagnostics: list[dict[str, object]]) -> dict[str, Any]:
    parse_diagnostics = [
        diagnostic.model_dump(mode="json")
        for file in files
        if file.parse_result
        for diagnostic in file.parse_result.diagnostics
    ]
    empty_files = [file.relative_path for file in files if file.size_bytes == 0]
    malformed_rows = sum(
        table.malformed_rows
        for file in files
        if file.parse_result
        for table in file.parse_result.tables
    )
    repeated_headers = sum(
        table.repeated_headers
        for file in files
        if file.parse_result
        for table in file.parse_result.tables
    )
    return {
        "file_count": len(files),
        "empty_files": empty_files,
        "malformed_rows": malformed_rows,
        "repeated_headers": repeated_headers,
        "parse_diagnostics": parse_diagnostics,
        "deduplication_diagnostics": dedupe_diagnostics,
        "operator_context_coverage": 0.0,
        "quality_outcome_coverage": 0.0,
        "notes": [
            "Empty error logs are not treated as proof that no problems occurred.",
            "Unknown fields and unmapped states are preserved for future enrichment.",
        ],
    }

