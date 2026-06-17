"""Machine parameter settings: every numeric used by time/cost estimation.

All values live in the single-row machine_params table and are edited via
the dashboard — calculation code must never hardcode them.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_prints_repository
from storage.repositories.prints_repo import _PARAM_FIELDS, PrintsRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])

_NUMERIC_FIELDS = {
    "hatch_speed_mm_s", "contour_speed_mm_s", "hatch_distance_mm", "layer_thickness_mm",
    "recoat_time_ms", "jump_speed_mm_s", "jump_delay_ms",
    "powder_cost_rub_per_kg", "gas_cost_rub_per_atm", "gas_atm_per_print",
    "filter_cost_rub", "filter_lifetime_hours", "platform_cost_rub",
    "build_area_cm2", "time_correction_factor",
}
_INT_FIELDS = {"laser_count"}
_DICT_FIELDS = {"material_densities", "hatch_speeds_by_mat"}

# Fields the time/cost estimators cannot work without
_REQUIRED_FOR_ESTIMATION = (
    "hatch_speed_mm_s", "contour_speed_mm_s", "hatch_distance_mm", "layer_thickness_mm", "laser_count",
)


def params_configured(params: dict | None) -> bool:
    """True when all estimation-critical parameters are filled in."""
    if not params:
        return False
    return all(params.get(field) is not None for field in _REQUIRED_FOR_ESTIMATION)


@router.get("/machine")
def get_machine_params(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Current machine parameters; unset fields are null."""
    params = repo.get_machine_params()
    if params is None:
        params = {field: None for field in _PARAM_FIELDS}
        params["material_densities"] = {}
        params["hatch_speeds_by_mat"] = {}
        params["updated_at"] = None
    return {"params": params, "configured": params_configured(params)}


@router.put("/machine")
def update_machine_params(
    payload: dict,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Partial update: only the fields present in the body are changed."""
    values: dict = {}
    for key, raw in payload.items():
        if key in _NUMERIC_FIELDS:
            if raw is None:
                values[key] = None
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(422, f"Поле '{key}' должно быть числом")
            if value < 0:
                raise HTTPException(422, f"Поле '{key}' не может быть отрицательным")
            values[key] = value
        elif key in _INT_FIELDS:
            if raw is None:
                values[key] = None
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(422, f"Поле '{key}' должно быть целым числом")
            if value < 1:
                raise HTTPException(422, f"Поле '{key}' должно быть ≥ 1")
            values[key] = value
        elif key in _DICT_FIELDS:
            if not isinstance(raw, dict):
                raise HTTPException(422, f"Поле '{key}' должно быть объектом")
            cleaned: dict[str, float] = {}
            for mat, num in raw.items():
                try:
                    cleaned[str(mat)] = float(num)
                except (TypeError, ValueError):
                    raise HTTPException(422, f"Значение '{key}.{mat}' должно быть числом")
            values[key] = cleaned
        # Unknown keys are ignored — keeps the endpoint forward-compatible

    if not values:
        raise HTTPException(422, "Нет известных полей для обновления")

    params = repo.save_machine_params(values)
    repo.flush()
    logger.info("machine_settings: updated %s", sorted(values.keys()))
    return {"params": params, "configured": params_configured(params)}
