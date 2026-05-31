import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from domain.enums.common import SourceChannel, VerificationStatus


def _extract_number(text: str, pattern: re.Pattern, unit: str | None = None) -> tuple[str | None, str | None]:
    """Safely extract numeric value from text using regex pattern."""
    match = pattern.search(text)
    if not match:
        return None, None
    try:
        value_str = match.group(1).replace(",", ".")
        value = float(value_str)
        if value == int(value):
            value_str = str(int(value))
        else:
            value_str = str(value)
        if match.lastindex and match.lastindex >= 2:
            return value_str, unit or match.group(2).lower()
        return value_str, None
    except (ValueError, AttributeError, IndexError):
        return None, None


class OperatorEventDraft(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "operator"
    source_channel: SourceChannel = SourceChannel.telegram
    event_type: str
    printer_id: str | None = None
    session_id: str | None = None
    build_id: str | None = None
    layer: int | None = None
    material: str | None = None
    powder_batch: str | None = None
    gas_type: str | None = None
    gas_cylinder_id: str | None = None
    component: str | None = None
    action: str | None = None
    value: str | None = None
    unit: str | None = None
    note: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.4
    verification_status: VerificationStatus = VerificationStatus.unverified
    needs_confirmation: bool = True
    parse_warnings: list[str] = Field(default_factory=list)


CYLINDER_RE = re.compile(r"(?:баллон|cylinder)\s*([A-Za-zА-Яа-я0-9_-]+)", re.IGNORECASE)
PRESSURE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(бар|bar|atm|атм)", re.IGNORECASE)
LAYER_RE = re.compile(r"(?:сло[йяеё]|layer)\s*[№#]?\s*(\d+)", re.IGNORECASE)
POWDER_BATCH_RE = re.compile(r"(?:партия[_\-\s]*([A-Za-zА-Яа-я0-9_-]+)|(batch[_\-\s]*[A-Za-zА-Яа-я0-9_-]+))", re.IGNORECASE)
MATERIAL_RE = re.compile(r"\b(AlSi10Mg|AlSi ?10Mg|316L|Ti6Al4V|Inconel ?718|сталь|титан|алюмин(?:ий|ия|иев))\b", re.IGNORECASE)
REMAINING_RE = re.compile(r"(?:остаток|осталось|ост\.?)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
CONSUMED_RE = re.compile(r"(?:потрач|израсход|использов|расход|потрачено)\s*[:=]?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|kg|грамм|g)", re.IGNORECASE)


def parse_operator_text(message: str, source_channel: SourceChannel = SourceChannel.telegram) -> OperatorEventDraft:
    text = message.strip()
    lower = text.lower()
    draft = OperatorEventDraft(event_type="operator_observation", source_channel=source_channel, note=text)

    if "баллон" in lower or "argon" in lower or "аргон" in lower or "газ" in lower:
        remaining_match = REMAINING_RE.search(text)
        consumed_match = CONSUMED_RE.search(text)
        is_new = any(token in lower for token in ("нов", "замен", "постав"))

        if remaining_match:
            draft.event_type = "gas_consumption_recorded"
            val, _ = _extract_number(text, REMAINING_RE)
            if val:
                draft.value = val
                draft.unit = "bar_remaining"
                draft.confidence += 0.30
        elif consumed_match:
            draft.event_type = "gas_consumption_recorded"
            val, _ = _extract_number(text, CONSUMED_RE)
            if val:
                draft.action = "consumed"
                draft.value = val
                draft.unit = "bar"
                draft.confidence += 0.30
        elif is_new:
            draft.event_type = "gas_cylinder_replaced"
        else:
            draft.event_type = "gas_pressure_issue"

        draft.gas_type = "argon" if "аргон" in lower or "argon" in lower else None
        cylinders = [match.group(1) for match in CYLINDER_RE.finditer(text)]
        cylinder_id = next((value for value in reversed(cylinders) if re.search(r"\d", value)), None)
        if cylinder_id:
            draft.gas_cylinder_id = cylinder_id
            draft.confidence += 0.25

        if not draft.value:
            val, unit = _extract_number(text, PRESSURE_RE)
            if val:
                draft.value = val
                draft.unit = unit
                draft.confidence += 0.15
    elif "порош" in lower or "powder" in lower:
        consumed_match = CONSUMED_RE.search(text)
        weight_match = WEIGHT_RE.search(text)

        if consumed_match or weight_match:
            draft.event_type = "powder_consumption_recorded"
            if consumed_match:
                val, _ = _extract_number(text, CONSUMED_RE)
                if val:
                    draft.value = val
                    draft.unit = "kg"
                    draft.confidence += 0.30
            if weight_match:
                val, unit = _extract_number(text, WEIGHT_RE)
                if val:
                    if unit in ("грамм", "g"):
                        val = str(float(val) / 1000.0)
                    draft.value = val
                    draft.unit = "kg"
                    draft.confidence += 0.30
        elif "сито" in lower or "просе" in lower:
            draft.event_type = "powder_sieved"
        elif "суш" in lower:
            draft.event_type = "powder_dried"
        elif "цикл" in lower or "reuse" in lower or "повтор" in lower:
            draft.event_type = "powder_reused"
        else:
            draft.event_type = "powder_batch_changed"

        batch = POWDER_BATCH_RE.search(text)
        if batch:
            draft.powder_batch = batch.group(1) or batch.group(2)
            draft.confidence += 0.25
        material = MATERIAL_RE.search(text)
        if material:
            draft.material = material.group(1)
            draft.confidence += 0.15
        reuse = re.search(r"(?:цикл|reuse cycle)\s*(\d+)|(\d+)\s*цикл", lower)
        if reuse and draft.event_type == "powder_reused":
            draft.value = next(group for group in reuse.groups() if group)
            draft.unit = "reuse_cycle"
    elif any(token in lower for token in ("уплотн", "filter", "фильтр", "оптик", "камер", "рекот", "калибр")):
        component_map = [
            ("уплотн", "seal", "seal_replaced"),
            ("фильтр", "filter", "filter_replaced"),
            ("filter", "filter", "filter_replaced"),
            ("оптик", "optics", "optics_cleaned"),
            ("камер", "chamber", "chamber_cleaned"),
            ("рекот", "recoater", "recoater_adjusted"),
            ("калибр", "calibration", "calibration_performed"),
        ]
        for token, component, event_type in component_map:
            if token in lower:
                draft.component = component
                draft.event_type = event_type
                draft.action = event_type
                draft.confidence += 0.30
                break
    elif "рестарт" in lower or "restart" in lower:
        draft.event_type = "restart_attempt"
        draft.action = "manual_restart"
        layer = LAYER_RE.search(text)
        if layer:
            draft.layer = int(layer.group(1))
            draft.confidence += 0.25
    elif any(token in lower for token in ("принята", "accepted", "годн")):
        draft.event_type = "part_accepted"
        draft.confidence += 0.25
    elif any(token in lower for token in ("забрак", "rejected", "дефект", "пор")):
        draft.event_type = "part_rejected" if "забрак" in lower or "rejected" in lower else "visual_defect_found"
        if "пор" in lower:
            draft.action = "porosity_observed"
            draft.confidence += 0.15
        draft.confidence += 0.20

    if not draft.material:
        material = MATERIAL_RE.search(text)
        if material:
            draft.material = material.group(1)
            draft.confidence += 0.15

    if draft.confidence >= 0.75:
        draft.verification_status = VerificationStatus.draft
        draft.needs_confirmation = False
    if draft.confidence < 0.55:
        draft.parse_warnings.append("Message is ambiguous and should be confirmed or clarified.")
    return draft
