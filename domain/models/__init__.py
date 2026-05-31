"""Domain models package.

Modular organization:
- sessions: Printer, BuildSession, BuildJob, Part, SourceFile, ImportJob, ReportArtifact
- events: OperatorEvent, OperatorJournalEntry, CanonicalEvent, StateTransition, Segment
- quality: QualityOutcome, Anomaly, MaintenanceRecord, GasCylinder, MaterialBatch
- insights: PatternInsight, Hypothesis, CausalLink, LLMRun

Backward compatibility:
- Original entities.py provides all models via re-exports
- Import from here or from domain.models works the same
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

from domain.models.insights import (
    CausalLink,
    ConfirmedKnowledge,
    HistoricalAnalysisVerdict,
    Hypothesis,
    LLMRun,
    PatternInsight,
)

__all__ = [
    # Sessions
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
    # Events
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
    # Quality
    "QualityOutcome",
    "Anomaly",
    "MaintenanceRecord",
    "ComponentStateTimeline",
    "GasCylinder",
    "MaterialBatch",
    "PowderUsageCycle",
    "PowderPreparationEvent",
    "Attachment",
    # Insights
    "PatternInsight",
    "ConfirmedKnowledge",
    "HistoricalAnalysisVerdict",
    "Hypothesis",
    "CausalLink",
    "LLMRun",
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)