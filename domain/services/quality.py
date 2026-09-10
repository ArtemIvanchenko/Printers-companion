from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from domain.enums.common import DefectType, QualityInspectionType, QualityResult


class QualityOutcomeDraft(BaseModel):
    outcome_id: str = Field(default_factory=lambda: f"quality_{uuid4().hex}")
    print_record_id: str | None = None
    session_id: str | None = None
    build_id: str | None = None
    part_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    inspection_type: QualityInspectionType = QualityInspectionType.visual
    result: QualityResult = QualityResult.unknown
    is_final: bool = False
    supersedes_outcome_id: str | None = None
    inspection_result: str | None = Field(default=None, max_length=4000)
    defect_type: DefectType | None = None
    defect_location: str | None = None
    layer_range: dict[str, int] | None = None
    severity: str | None = None
    notes: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str = "operator"
    evidence_links: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("inspection_result", "defect_location", "notes", mode="before")
    @classmethod
    def _strip_optional_text(cls, value):
        if value is None:
            return None
        clean = str(value).strip()
        return clean or None


class FinalPrintOutcomeDraft(QualityOutcomeDraft):
    """A training-grade, operator-confirmed good/defect label.

    The generic quality endpoint still supports interim ``warning``/``unknown``
    observations.  A final print-card label is deliberately stricter because
    these rows become ground truth for the defect model.
    """

    result: QualityResult

    @model_validator(mode="after")
    def _validate_final_label(self):
        if self.result not in {QualityResult.accepted, QualityResult.rejected}:
            raise ValueError("итог печати должен быть 'accepted' или 'rejected'")
        if not self.inspection_result:
            raise ValueError("укажите результат контроля")
        if self.result == QualityResult.rejected:
            if self.defect_type is None:
                raise ValueError("для брака укажите тип дефекта")
            if not self.defect_location and not self.layer_range:
                raise ValueError("для брака укажите расположение дефекта или диапазон слоёв")
        return self


def create_quality_outcome(payload: dict[str, Any]) -> QualityOutcomeDraft:
    # Generic API observations are never allowed to self-promote into ML
    # ground truth. Only create_final_print_outcome performs strict validation
    # and sets this flag. IDs are server-owned as well: accepting a caller's ID
    # would let the repository mistake a create request for an update of an
    # existing (possibly final) inspection.
    return QualityOutcomeDraft(**{
        **payload,
        "outcome_id": f"quality_{uuid4().hex}",
        "is_final": False,
        "supersedes_outcome_id": None,
    })


def create_final_print_outcome(
    payload: dict[str, Any],
    *,
    print_record_id: str,
    session_id: str | None,
    created_by: str,
) -> FinalPrintOutcomeDraft:
    """Build a final print label while preventing client-spoofed ownership."""
    values = {
        **payload,
        # Final labels are append-only. Never allow a client-supplied id to
        # overwrite an earlier inspection through the repository upsert path.
        "outcome_id": f"quality_{uuid4().hex}",
        "print_record_id": print_record_id,
        "session_id": session_id,
        "created_by": created_by,
        "is_final": True,
        # This field orders the immutable audit chain and therefore must be a
        # server observation time, not a caller-controlled value. If the actual
        # inspection time later matters separately, it needs its own field.
        "timestamp": datetime.now(timezone.utc),
    }
    return FinalPrintOutcomeDraft(**values)
