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
_DICT_FIELDS = {"material_densities", "hatch_speeds_by_mat", "time_correction_by_mat"}
_BOOL_FIELDS = {"correction_locked"}

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
        params["time_correction_by_mat"] = {}
        params["correction_locked"] = False
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
        elif key in _BOOL_FIELDS:
            values[key] = bool(raw)
        # Unknown keys are ignored — keeps the endpoint forward-compatible

    # Manually editing a correction factor pins it: auto-calibration must not
    # overwrite the operator's value unless they explicitly unlock.
    if ("time_correction_by_mat" in values or "time_correction_factor" in values) \
            and "correction_locked" not in values:
        values["correction_locked"] = True

    if not values:
        raise HTTPException(422, "Нет известных полей для обновления")

    params = repo.save_machine_params(values)
    repo.flush()
    logger.info("machine_settings: updated %s", sorted(values.keys()))
    return {"params": params, "configured": params_configured(params)}


# ── Material scanning presets ──────────────────────────────────────────────────

_PRESET_NUMERIC = {
    "layer_thickness_mm", "hatch_speed_mm_s", "contour_speed_mm_s",
    "hatch_distance_mm", "jump_speed_mm_s", "jump_delay_ms", "laser_power_w",
}


def _validate_preset_payload(payload: dict) -> dict:
    values: dict = {}
    for key, raw in payload.items():
        if key == "name":
            name = (raw or "").strip()
            if not name:
                raise HTTPException(422, "Поле 'name' не может быть пустым")
            values["name"] = name
        elif key == "material":
            mat = (raw or "").strip().lower()
            if not mat:
                raise HTTPException(422, "Поле 'material' не может быть пустым")
            values["material"] = mat
        elif key in _PRESET_NUMERIC:
            if raw is None:
                values[key] = None
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(422, f"Поле '{key}' должно быть числом")
            if v < 0:
                raise HTTPException(422, f"Поле '{key}' не может быть отрицательным")
            values[key] = v
        elif key == "is_default":
            values["is_default"] = bool(raw)
        elif key == "notes":
            values["notes"] = (raw or "").strip() or None
    return values


@router.get("/presets")
def list_presets(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """All material scanning presets."""
    return {"presets": repo.list_presets()}


@router.post("/presets")
def create_preset(payload: dict, repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Create a material scanning preset."""
    if "name" not in payload or "material" not in payload:
        raise HTTPException(422, "Поля 'name' и 'material' обязательны")
    values = _validate_preset_payload(payload)
    preset = repo.create_preset(values)
    repo.flush()
    logger.info("machine_settings: created preset %s (%s)", preset["name"], preset["material"])
    return {"preset": preset}


@router.get("/presets/material/{material}")
def get_preset_for_material(
    material: str, repo: PrintsRepository = Depends(get_prints_repository)
) -> dict:
    """Active (default) preset for the given material; null when none configured."""
    return {"preset": repo.get_active_preset_for_material(material)}


@router.put("/presets/{preset_id}")
def update_preset(
    preset_id: int, payload: dict, repo: PrintsRepository = Depends(get_prints_repository)
) -> dict:
    """Partial update of a preset."""
    values = _validate_preset_payload(payload)
    if not values:
        raise HTTPException(422, "Нет известных полей для обновления")
    preset = repo.update_preset(preset_id, values)
    if not preset:
        raise HTTPException(404, "Пресет не найден")
    repo.flush()
    return {"preset": preset}


@router.post("/presets/{preset_id}/set-default")
def set_default_preset(
    preset_id: int, repo: PrintsRepository = Depends(get_prints_repository)
) -> dict:
    """Mark this preset as the default for its material."""
    preset = repo.set_default_preset(preset_id)
    if not preset:
        raise HTTPException(404, "Пресет не найден")
    repo.flush()
    return {"preset": preset}


@router.delete("/presets/{preset_id}")
def delete_preset(
    preset_id: int, repo: PrintsRepository = Depends(get_prints_repository)
) -> dict:
    """Delete a material scanning preset."""
    if not repo.delete_preset(preset_id):
        raise HTTPException(404, "Пресет не найден")
    repo.flush()
    return {"deleted": preset_id}
