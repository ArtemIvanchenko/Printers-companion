"""Cost estimation for SLM prints.

All rates come from machine_params (operator-editable); the powder price can
be overridden per print with the snapshot stored on the PrintRecord. Missing
parameters drop their line item and produce a warning instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from analytics.prediction.print_time import PrintTimeEstimate
from analytics.prediction.stl_slicer import SliceResult


@dataclass
class CostEstimate:
    total_rub: float
    powder_kg: float | None
    breakdown: dict = field(default_factory=dict)   # статья → руб
    warnings: list[str] = field(default_factory=list)


def estimate_cost(
    slices: SliceResult,
    params: dict,
    material: str,
    time_estimate: PrintTimeEstimate,
    powder_cost_override: float | None = None,
) -> CostEstimate:
    breakdown: dict[str, float] = {}
    warnings: list[str] = []

    # Порошок: масса детали = объём × плотность (поддержки/просыпь не учтены)
    density = (params.get("material_densities") or {}).get(material)
    powder_kg: float | None = None
    if density:
        powder_kg = slices.volume_mm3 / 1000.0 * float(density) / 1000.0  # мм³→см³→кг
        powder_rate = powder_cost_override or params.get("powder_cost_rub_per_kg")
        if powder_rate:
            breakdown["порошок"] = round(powder_kg * float(powder_rate), 2)
        else:
            warnings.append("Не задана цена порошка — статья не учтена.")
    else:
        warnings.append(f"Не задана плотность материала '{material}' — порошок не учтён.")

    gas_rate, gas_amount = params.get("gas_cost_rub_per_atm"), params.get("gas_atm_per_print")
    if gas_rate and gas_amount:
        breakdown["газ"] = round(float(gas_rate) * float(gas_amount), 2)
    else:
        warnings.append("Не заданы цена/расход газа — статья не учтена.")

    filter_cost, filter_life = params.get("filter_cost_rub"), params.get("filter_lifetime_hours")
    if filter_cost and filter_life:
        breakdown["фильтр"] = round(time_estimate.print_hours / float(filter_life) * float(filter_cost), 2)
    else:
        warnings.append("Не заданы цена/ресурс фильтра — статья не учтена.")

    platform = params.get("platform_cost_rub")
    if platform:
        breakdown["платформа"] = round(float(platform), 2)
    else:
        warnings.append("Не задана стоимость обработки платформы — статья не учтена.")

    return CostEstimate(
        total_rub=round(sum(breakdown.values()), 2),
        powder_kg=round(powder_kg, 3) if powder_kg is not None else None,
        breakdown=breakdown,
        warnings=warnings,
    )


__all__ = ["CostEstimate", "estimate_cost"]
