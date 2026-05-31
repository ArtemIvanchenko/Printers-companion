from pydantic import BaseModel, Field

from domain.enums.common import SessionClassification, SourceFileFamily
from domain.services.ingestion import IngestedFile


class SessionClassificationResult(BaseModel):
    classification: SessionClassification
    confidence: float
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def classify_session(files: list[IngestedFile], operator_event_types: list[str] | None = None) -> SessionClassificationResult:
    operator_event_types = operator_event_types or []
    families = {file.classification.family for file in files}
    events = [
        event
        for file in files
        if file.parse_result
        for event in file.parse_result.events
    ]
    transitions = [
        transition
        for file in files
        if file.parse_result
        for transition in file.parse_result.transitions
    ]

    has_burn_rows = any(
        file.parse_result
        and file.classification.family == SourceFileFamily.burn_log
        and any(table.rows for table in file.parse_result.tables)
        for file in files
    )
    has_layer_or_start = any(event.event_type in {"start", "burn_event", "layer_reference"} for event in events)
    has_restart = any(event.event_type in {"restart_attempt", "resume"} for event in events)
    has_pause = any(event.event_type == "pause" for event in events)
    has_service_operator = any(event.startswith(("filter_", "seal_", "optics_", "chamber_", "calibration_", "software_")) for event in operator_event_types)
    has_service_state = any(
        transition.subsystem in {"door", "glove", "parameter_check", "heating", "power"}
        for transition in transitions
    )
    monitor_only = families and families <= {SourceFileFamily.monitor100_log, SourceFileFamily.monitor200_log}

    if has_burn_rows or has_layer_or_start:
        evidence = []
        if has_burn_rows:
            evidence.append("burn log contains layer/process rows")
        if has_layer_or_start:
            evidence.append("main event/time evidence contains print/layer references")
        if has_restart or (has_pause and any(event.event_type == "resume" for event in events)):
            evidence.append("pause/resume or restart evidence present")
            return SessionClassificationResult(
                classification=SessionClassification.real_print_with_resume,
                confidence=0.82,
                evidence=evidence,
            )
        return SessionClassificationResult(
            classification=SessionClassification.real_print,
            confidence=0.78,
            evidence=evidence,
        )
    if has_service_operator or (has_service_state and not has_burn_rows):
        return SessionClassificationResult(
            classification=SessionClassification.service_session,
            confidence=0.70 if has_service_operator else 0.55,
            evidence=["maintenance/service evidence without layer-oriented print data"],
            warnings=["Service classification remains provisional without operator confirmation."] if not has_service_operator else [],
        )
    if monitor_only:
        return SessionClassificationResult(
            classification=SessionClassification.idle_diagnostic,
            confidence=0.50,
            evidence=["Monitor activity without print layers is not enough to infer a real print."],
        )
    if SourceFileFamily.main_event_log in families:
        return SessionClassificationResult(
            classification=SessionClassification.pre_burn_session,
            confidence=0.45,
            evidence=["Main log exists but no burn/layer process data was found."],
        )
    return SessionClassificationResult(
        classification=SessionClassification.incomplete_or_unknown,
        confidence=0.20,
        evidence=["Insufficient multi-source evidence for a stronger classification."],
        warnings=["Empty error logs, if present, are not treated as absence of problems."],
    )

