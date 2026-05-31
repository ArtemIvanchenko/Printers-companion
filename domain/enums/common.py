from enum import StrEnum


class FileRole(StrEnum):
    primary = "primary"
    secondary = "secondary"
    auxiliary = "auxiliary"
    unknown = "unknown"


class SourceFileFamily(StrEnum):
    main_event_log = "main_event_log"
    burn_log = "burn_log"
    time_log = "time_log"
    sensors_log = "sensors_log"
    monitor100_log = "monitor100_log"
    monitor200_log = "monitor200_log"
    stateflow_log = "stateflow_log"
    stateflowdata_log = "stateflowdata_log"
    table_temp_log = "table_temp_log"
    error_log = "error_log"
    unsupported = "unsupported"


class DataQualityStatus(StrEnum):
    ok = "ok"
    empty = "empty"
    zero_byte = "zero_byte"
    malformed = "malformed"
    partial_recovery = "partial_recovery"
    unsupported = "unsupported"
    binary_or_unknown = "binary_or_unknown"


class EvidenceKind(StrEnum):
    machine_log = "machine_log"
    operator_event = "operator_event"
    quality_outcome = "quality_outcome"
    derived_feature = "derived_feature"
    rule_result = "rule_result"
    statistical_result = "statistical_result"


class SessionClassification(StrEnum):
    real_print = "REAL_PRINT"
    real_print_with_resume = "REAL_PRINT_WITH_RESUME"
    service_session = "SERVICE_SESSION"
    pre_burn_session = "PRE_BURN_SESSION"
    idle_diagnostic = "IDLE_DIAGNOSTIC"
    maintenance_window = "MAINTENANCE_WINDOW"
    incomplete_or_unknown = "INCOMPLETE_OR_UNKNOWN"


class SourceChannel(StrEnum):
    web_ui = "web_ui"
    telegram = "telegram"
    openclaw = "openclaw"
    api = "api"
    manual_import = "manual_import"


class VerificationStatus(StrEnum):
    draft = "draft"
    unverified = "unverified"
    operator_confirmed = "operator_confirmed"
    supervisor_confirmed = "supervisor_confirmed"
    system_matched = "system_matched"
    dismissed = "dismissed"


class QualityInspectionType(StrEnum):
    visual = "visual"
    dimensional = "dimensional"
    ct = "ct"
    metallography = "metallography"
    tensile = "tensile"
    density = "density"
    hardness = "hardness"
    surface_roughness = "surface_roughness"
    other = "other"


class QualityResult(StrEnum):
    accepted = "accepted"
    rejected = "rejected"
    warning = "warning"
    unknown = "unknown"


class DefectType(StrEnum):
    porosity = "porosity"
    lack_of_fusion = "lack_of_fusion"
    crack = "crack"
    warping = "warping"
    surface_defect = "surface_defect"
    delamination = "delamination"
    oxidation = "oxidation"
    powder_inclusion = "powder_inclusion"
    dimensional_deviation = "dimensional_deviation"
    other = "other"


class RelationshipType(StrEnum):
    precedes = "precedes"
    temporally_near = "temporally_near"
    correlates_with = "correlates_with"
    likely_contributes_to = "likely_contributes_to"
    likely_causes = "likely_causes"
    contradicted_by = "contradicted_by"
    insufficient_evidence_for = "insufficient_evidence_for"


class InsightStatus(StrEnum):
    draft = "draft"
    needs_review = "needs_review"
    active = "active"
    dismissed = "dismissed"
    confirmed = "confirmed"
    superseded = "superseded"
    monitoring = "monitoring"


class HistoricalAnalysisStatus(StrEnum):
    completed = "completed"
    skipped = "skipped"
    failed = "failed"
    insufficient_data = "insufficient_data"
    needs_human_input = "needs_human_input"


class HistoricalVerdict(StrEnum):
    new_pattern_found = "new_pattern_found"
    no_new_pattern = "no_new_pattern"
    weak_signal_found = "weak_signal_found"
    insufficient_data = "insufficient_data"
    contradictory_evidence = "contradictory_evidence"


class ImportJobStatus(StrEnum):
    detected = "detected"
    awaiting_operator_confirmation = "awaiting_operator_confirmation"
    postponed = "postponed"
    ignored = "ignored"
    checking_stability = "checking_stability"
    importing = "importing"
    analyzing = "analyzing"
    reporting = "reporting"
    done = "done"
    failed = "failed"
    needs_operator_context = "needs_operator_context"
