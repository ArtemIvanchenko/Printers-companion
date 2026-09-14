"""Transport-independent validation for operator print-card fields."""

from datetime import datetime, timezone
from domain.services.print_cards.contracts import CardError


def clean_material(raw: str | None) -> str:
    material = (raw or "").strip().lower()
    if not material:
        raise CardError("invalid_inputs", "Поле 'material' не может быть пустым")
    if len(material) > 120:
        raise CardError("invalid_inputs", "Поле 'material' слишком длинное (макс. 120)")
    return material


def parse_iso_datetime(raw, field: str) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        raise CardError("invalid_inputs", f"Поле '{field}' должно быть датой ISO (ГГГГ-ММ-ДД)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_powder_cost(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise CardError("invalid_inputs", "Поле 'powder_cost_rub_per_kg' должно быть числом")
    if value < 0:
        raise CardError("invalid_inputs", "Цена порошка не может быть отрицательной")
    return value


def parse_layer_thickness(raw) -> float | None:
    """Layer thickness in mm, or None for "use the machine default"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise CardError("invalid_inputs", "Поле 'layer_thickness_mm' должно быть числом")
    # Loose bounds: SLM layers run roughly 0.02–0.1 mm, but the guard only has
    # to reject nonsense (a value in microns, a negative) — the exact process
    # window is the operator's call, not this endpoint's.
    if not (0.0 < value <= 1.0):
        raise CardError("invalid_inputs", "Толщина слоя должна быть в мм, в диапазоне 0–1")
    return value


def parse_hatch_distance(raw) -> float | None:
    """Hatch distance in mm, or None for "use the material preset"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise CardError("invalid_inputs", "Поле 'hatch_distance_mm' должно быть числом")
    # This machine's own logs show applied values from 0.10 to 0.90 mm, and the
    # operator briefly typed 3.00 while editing, so the window is genuinely
    # wide. The guard only rejects nonsense (microns, a negative).
    if not (0.0 < value <= 5.0):
        raise CardError("invalid_inputs", "Шаг штриховки должен быть в мм, в диапазоне 0–5")
    return value
