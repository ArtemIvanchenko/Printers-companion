from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from domain.enums.common import DefectType, QualityInspectionType, QualityResult


def final_outcome_head(outcomes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One complete strict audit lineage, never a wall-clock guess.

    None means no final or an unverifiable chain. Callers retain all history;
    a writer distinguishes these states by checking whether finals exist.
    """
    finals = [row for row in outcomes if row.get("is_final") is True]
    if not finals:
        return None
    nodes = {}
    children = {}
    for row in finals:
        identity = row.get("outcome_id")
        if not isinstance(identity, str) or not identity or identity in nodes:
            return None
        nodes[identity] = row
    roots = []
    for identity, row in nodes.items():
        parent = row.get("supersedes_outcome_id")
        if parent is None:
            roots.append(identity)
        elif not isinstance(parent, str) or parent not in nodes or parent in children:
            return None  # Missing predecessor or two children (audit fork).
        else:
            children[parent] = identity
    heads = set(nodes) - children.keys()
    if len(roots) != 1 or len(heads) != 1:
        return None
    head = nodes[next(iter(heads))]
    visited = set()
    current = head
    while current is not None:
        identity = current["outcome_id"]
        if identity in visited:
            return None
        visited.add(identity)
        parent = current.get("supersedes_outcome_id")
        current = nodes[parent] if parent is not None else None
    # A sole terminal does not rule out a disconnected cycle elsewhere.
    return head if len(visited) == len(nodes) else None


def final_outcome_heads(outcomes: list[dict[str, Any]], *, card_sessions=None) -> list[dict[str, Any]]:
    """Resolve entire card histories before assigning their selected session.

    Cardless stored legacy finals support a single complete session lineage.
    Disconnected heads and cross-scope references fail closed, not by date.
    """
    groups = {}
    identities = {}
    invalid = set()
    for row in outcomes:
        if row.get("is_final") is not True:
            continue
        card = row.get("print_record_id")
        scope = ("card", card) if card else ("session", row.get("session_id"))
        groups.setdefault(scope, []).append(row)
        identity = row.get("outcome_id")
        if isinstance(identity, str) and identity:
            if identity in identities:
                invalid.update((scope, identities[identity]))
            identities[identity] = scope
    for scope, rows in groups.items():
        for row in rows:
            parent = row.get("supersedes_outcome_id")
            parent_scope = identities.get(parent) if isinstance(parent, str) else None
            if parent is not None and parent_scope != scope:
                invalid.add(scope)
                if parent_scope is not None:
                    invalid.add(parent_scope)
    heads = {}
    for scope, rows in groups.items():
        head = final_outcome_head(rows)
        if scope in invalid or head is None:
            invalid.add(scope)
        else:
            heads[scope] = head

    def linked_session(row):
        # Only a caller's current SQL card link can resolve a missing session;
        # never override an already recorded historical non-null association.
        return row.get("session_id") or (card_sessions or {}).get(row.get("print_record_id"))

    blocked = {linked_session(row) for scope in invalid for row in groups[scope]
               if linked_session(row)}
    session_counts = {}
    for head in heads.values():
        sid = linked_session(head)
        if sid:
            session_counts[sid] = session_counts.get(sid, 0) + 1
    return [head for head in heads.values()
            if not (sid := linked_session(head))
            or (sid not in blocked and session_counts[sid] == 1)]


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

    @field_validator("layer_range", mode="wrap")
    @classmethod
    def _validate_supported_layer_range(cls, value, handler):
        coordinates = ("start_layer", "end_layer")
        supported = isinstance(value, Mapping) and any(key in value for key in coordinates)
        if supported:
            if not all(key in value for key in coordinates):
                raise ValueError("диапазон слоёв должен содержать start_layer и end_layer")
            if any(isinstance(value[key], bool) for key in coordinates):
                raise ValueError("номера физических слоёв должны быть целыми числами")
        # Retain the existing integer coercion and arbitrary annotation keys.
        # It already rejects fractional/nonfinite values without guessing them.
        parsed = handler(value)
        if supported and not 1 <= parsed["start_layer"] <= parsed["end_layer"]:
            raise ValueError(
                "диапазон физических слоёв должен удовлетворять 1 <= start_layer <= end_layer"
            )
        return parsed

    @model_validator(mode="after")
    def _validate_final_label(self):
        if self.result not in {QualityResult.accepted, QualityResult.rejected}:
            raise ValueError("итог печати должен быть 'accepted' или 'rejected'")
        if not self.inspection_result:
            raise ValueError("укажите результат контроля")
        if self.result == QualityResult.rejected:
            if self.defect_type is None:
                raise ValueError("для брака укажите тип дефекта")
            supported_range = self.layer_range and all(
                key in self.layer_range for key in ("start_layer", "end_layer")
            )
            if not self.defect_location and not supported_range:
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
        # Server-observed UTC evidence for future-label validation, never a
        # caller-controlled inspection date. Audit order comes from supersedes,
        # not wall clocks of separate operator PCs.
        "timestamp": datetime.now(timezone.utc),
    }
    return FinalPrintOutcomeDraft(**values)
