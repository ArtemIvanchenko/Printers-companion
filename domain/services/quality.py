from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from domain.enums.common import DefectType, QualityInspectionType, QualityResult


class QualityOutcomeDraft(BaseModel):
    outcome_id: str = Field(default_factory=lambda: f"quality_{uuid4().hex}")
    session_id: str | None = None
    build_id: str | None = None
    part_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    inspection_type: QualityInspectionType = QualityInspectionType.visual
    result: QualityResult = QualityResult.unknown
    defect_type: DefectType | None = None
    defect_location: str | None = None
    layer_range: dict[str, int] | None = None
    severity: str | None = None
    notes: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str = "operator"
    evidence_links: list[dict[str, Any]] = Field(default_factory=list)


def create_quality_outcome(payload: dict[str, Any]) -> QualityOutcomeDraft:
    return QualityOutcomeDraft(**payload)

