"""Domain models - backward compatibility module.

This module re-exports all models from the modular subpackages.
For new code, import directly from domain.models.sessions, .events, .quality, .insights

Example:
    # Old way (still works):
    from domain.models.entities import BuildSession, OperatorEvent

    # New way (preferred):
    from domain.models import BuildSession, OperatorEvent
    # or:
    from domain.models.sessions import BuildSession
    from domain.models.events import OperatorEvent
"""
from datetime import datetime, timezone
from uuid import uuid4

from domain.models.sessions import (
    BuildJob,
    BuildPlate,
    BuildSession,
    ImportJob,
    LayerRange,
    Part,
    PartPlacement,
    Printer,
    PrinterProfile,
    ProfileVersion,
    ReportArtifact,
    SourceFile,
    ToleranceRule,
    AnalysisVersion,
)

from domain.models.events import (
    CanonicalEvent,
    LayerSnapshot,
    NotificationOutbox,
    OperatorEvent,
    OperatorEventAuditRecord,
    OperatorJournalEntry,
    ParseDiagnostic,
    ProductionContextSnapshot,
    Segment,
    SignalDefinition,
    StateTransition,
    UnknownSignalReport,
)

from domain.models.quality import (
    Anomaly,
    Attachment,
    ComponentStateTimeline,
    GasCylinder,
    MaintenanceRecord,
    MaterialBatch,
    PowderPreparationEvent,
    PowderUsageCycle,
    QualityOutcome,
)

from domain.models.prints import (
    MachineParams,
    MachinePreset,
    PrintRecord,
    PrintRecordFile,
)

from domain.models.insights import (
    CausalLink,
    ConfirmedKnowledge,
    HistoricalAnalysisVerdict,
    Hypothesis,
    LLMRun,
    PatternInsight,
)

__all__ = [
    "BuildSession",
    "BuildJob", 
    "Part",
    "BuildPlate",
    "PartPlacement",
    "LayerRange",
    "SourceFile",
    "ImportJob",
    "PrinterProfile",
    "ProfileVersion",
    "Printer",
    "ReportArtifact",
    "ToleranceRule",
    "AnalysisVersion",
    "OperatorEvent",
    "OperatorJournalEntry",
    "CanonicalEvent",
    "StateTransition",
    "LayerSnapshot",
    "Segment",
    "NotificationOutbox",
    "ParseDiagnostic",
    "OperatorEventAuditRecord",
    "ProductionContextSnapshot",
    "SignalDefinition",
    "UnknownSignalReport",
    "QualityOutcome",
    "Anomaly",
    "MaintenanceRecord",
    "ComponentStateTimeline",
    "GasCylinder",
    "MaterialBatch",
    "PowderUsageCycle",
    "PowderPreparationEvent",
    "Attachment",
    "PatternInsight",
    "ConfirmedKnowledge",
    "HistoricalAnalysisVerdict",
    "Hypothesis",
    "CausalLink",
    "LLMRun",
    "PrintRecord",
    "PrintRecordFile",
    "MachineParams",
    "MachinePreset",
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)