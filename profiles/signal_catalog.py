"""User-facing signal metadata for printer profiles.

Raw controller names (``SO1``, ``Flow T`` and similar) are stable ingestion
identifiers.  They must not leak into the operator UI as the only label, but
they also must not be renamed in parsed data.  This module is the single
translation boundary between those two concerns.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from profiles.base.profile import load_yaml

_M350_SIGNALS = Path(__file__).resolve().parent / "m350" / "signals.yaml"

_FAMILY_NAMES_RU = {
    "ST": "Температурный канал",
    "SO": "Канал кислорода",
    "SP": "Канал давления",
    "SF": "Канал расхода",
    "V": "Состояние клапана",
    "F": "Состояние фильтра",
    "BI": "Дискретный вход",
}

_UNIT_NAMES_RU = {
    "percent": "%",
    "degC": "°C",
    "bar": "бар",
    "bar_or_atm": "условные единицы давления",
    "raw_sensor_units": "неподтверждённые единицы",
    "signed_encoder_units": "импульсы энкодера",
    "steps": "шаги",
    "count": "шт.",
    "arbitrary": "условные единицы",
}


@lru_cache(maxsize=8)
def load_signal_catalog(path: str | None = None) -> dict[str, dict[str, Any]]:
    """Return the raw-name keyed signal catalog from the selected profile."""
    config_path = Path(path) if path else _M350_SIGNALS
    return load_yaml(config_path).get("signals", {}) or {}


def signal_metadata(raw_name: str) -> dict[str, Any]:
    """Return JSON-safe metadata, including a readable fallback for unknowns."""
    source = dict(load_signal_catalog().get(raw_name) or {})
    family = next((prefix for prefix in _FAMILY_NAMES_RU if raw_name.startswith(prefix)), None)
    display_name = source.get("display_name_ru")
    if not display_name:
        generic = _FAMILY_NAMES_RU.get(family, "Неопознанный сигнал")
        display_name = f"{generic} {raw_name}"

    unit = source.get("unit")
    source.update({
        "raw_name": raw_name,
        "display_name_ru": display_name,
        "short_name_ru": source.get("short_name_ru") or display_name,
        "description_ru": source.get("description_ru") or source.get("notes") or "",
        "unit_display_ru": source.get("unit_display_ru") or _UNIT_NAMES_RU.get(unit, unit or ""),
        "is_confirmed": str(source.get("active_status", "")).startswith("confirmed"),
    })
    return source


def signal_display_name(raw_name: str, *, include_code: bool = False) -> str:
    """Readable Russian label while retaining the controller code on demand."""
    label = str(signal_metadata(raw_name)["display_name_ru"])
    return f"{label} ({raw_name})" if include_code else label


def signal_labels_ru() -> dict[str, str]:
    """Compact mapping intended for API responses and dashboard JavaScript."""
    return {raw: signal_display_name(raw) for raw in load_signal_catalog()}


def enrich_signal_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Add display metadata without changing the raw-name keyed data contract."""
    return {
        raw_name: ({**value, "signal": signal_metadata(raw_name)}
                   if isinstance(value, dict) else value)
        for raw_name, value in stats.items()
    }


__all__ = [
    "enrich_signal_stats",
    "load_signal_catalog",
    "signal_display_name",
    "signal_labels_ru",
    "signal_metadata",
]
