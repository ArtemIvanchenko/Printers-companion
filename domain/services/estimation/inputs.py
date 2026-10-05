"""Read detached print/geometry/parameter inputs and their provenance."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from core.config.settings import get_settings
from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from domain.services.estimation.contracts import EstimateError, PreparedEstimate
from storage.repositories.prints_repo import PrintsRepository

_PRESET_SCANNING_KEYS = (
    "hatch_speed_mm_s",
    "contour_speed_mm_s",
    "hatch_distance_mm",
    "layer_thickness_mm",
    "jump_speed_mm_s",
    "jump_delay_ms",
)
_PRINT_SCANNING_KEYS = (*_PRESET_SCANNING_KEYS, "support_speed_mm_s", "laser_count",
                        "contours_enabled", "hatch_angle_deg")


# Fields the time/cost estimators cannot work without
_REQUIRED_FOR_ESTIMATION = (
    "hatch_speed_mm_s",
    "contour_speed_mm_s",
    "hatch_distance_mm",
    "layer_thickness_mm",
    "laser_count",
)

# Human-readable names for the "what is missing" message. Telling the operator
# to "fill in the machine parameters" when four of five are already filled by a
# preset is not actionable — the whole estimate was blocked on laser_count
# alone, which no preset supplies.
_FIELD_LABELS = {
    "hatch_speed_mm_s": "скорость штриховки",
    "contour_speed_mm_s": "скорость контуров",
    "hatch_distance_mm": "шаг штриховки",
    "layer_thickness_mm": "толщина слоя",
    "laser_count": "количество лазеров",
}

# Conservative configured fallback, NOT a statement about the physical machine.
# The operator's MasterSLM screenshots show two channels and per-model assignment;
# channel capacity alone does not prove a balanced two-laser scan.
_DEFAULT_LASER_COUNT = 1


def effective_params(params: dict | None) -> dict:
    """Machine params with defaults applied for fields that have a known one."""
    out = dict(params or {})
    if out.get("laser_count") is None:
        out["laser_count"] = _DEFAULT_LASER_COUNT
    return out


def missing_for_estimation(params: dict | None) -> list[str]:
    """Names of the estimation-critical fields that are still empty."""
    filled = effective_params(params)
    return [_FIELD_LABELS.get(f, f) for f in _REQUIRED_FOR_ESTIMATION
            if filled.get(f) is None
            and not (f == "contour_speed_mm_s" and filled.get("contours_enabled") is False)]


def params_configured(params: dict | None) -> bool:
    """True when all estimation-critical parameters are filled in."""
    return not missing_for_estimation(params)


def require_local_print(record: dict, *, compute_node_id: str | None = None) -> None:
    node_id = compute_node_id or get_settings().compute_node_id
    try:
        require_compute_owner(
            entity_type="print_record",
            entity_id=str(record["record_id"]),
            origin_compute_node_id=str(record["origin_compute_node_id"]),
            requested_compute_node_id=node_id,
        )
    except ComputeAffinityError as exc:
        raise EstimateError(
            "forbidden",
            f"Расчёт и загрузка исходных файлов разрешены только на ПК-владельце карточки. {exc}",
        ) from exc


def params_for_record(repo: PrintsRepository, record: dict) -> dict:
    return _params_with_sources_for_record(repo, record)[0]


def _params_with_sources_for_record(repo: PrintsRepository, record: dict) -> tuple[dict, dict]:
    """Scanning parameters for one print, most specific source winning.

    machine_params (global) < material preset < print fields < print scan strategy.

    Both per-print overrides exist because this shop changes them per job while
    the machine holds one global value:

    * ``layer_thickness_mm`` is part of the captured scan scope, together with
      the physical machine and other configured scan parameters. Fitted models
      must not transfer across incompatible scopes.
    * ``hatch_distance_mm`` rescales scan length approximately as 1/hatch.
      Its value must come from a known job strategy or operator configuration;
      unlabelled Monitor100 positions do not establish its meaning or units.

    NULL on the record means "not specified": fall through to the preset, then
    to the machine default, so records that predate these fields are unaffected.
    """
    params = dict(repo.get_machine_params() or {})
    sources = {
        key: {"source": "machine_settings", "value": params[key]}
        for key in ("layer_thickness_mm", "hatch_distance_mm")
        if params.get(key) is not None
    }
    preset = repo.get_active_preset_for_material(record["material"])
    if preset:
        params["active_preset_id"] = str(preset["preset_id"]) if preset.get("preset_id") else None
        params.update(
            {k: v for k, v in preset.items() if k in _PRESET_SCANNING_KEYS and v is not None}
        )
        for key in ("layer_thickness_mm", "hatch_distance_mm"):
            if preset.get(key) is not None:
                sources[key] = {
                    "source": "material_preset",
                    "value": preset[key],
                    "preset_id": preset.get("preset_id"),
                }
    for field in ("layer_thickness_mm", "hatch_distance_mm"):
        if record.get(field):
            params[field] = record[field]
            sources[field] = {"source": "print_record", "value": record[field]}
    strategy = (record.get("metadata_json") or {}).get("scan_strategy")
    if strategy is None:
        strategy = {}
    if not isinstance(strategy, dict):
        raise EstimateError("invalid_inputs", "scan_strategy карточки должен быть объектом")
    for key in _PRINT_SCANNING_KEYS:
        if key in strategy:
            value = strategy[key]
            if key != "contours_enabled":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EstimateError("invalid_inputs", f"Недопустимый параметр scan_strategy: {key}")
                try:
                    value = float(value)
                except OverflowError as exc:
                    raise EstimateError(
                        "invalid_inputs", f"Недопустимый параметр scan_strategy: {key}",
                    ) from exc
                if (not math.isfinite(value)
                        or (key == "jump_delay_ms" and value < 0)
                        or (key not in {"hatch_angle_deg", "jump_delay_ms"} and value <= 0)
                        or (key == "laser_count" and int(value) != value)):
                    raise EstimateError("invalid_inputs", f"Недопустимый параметр scan_strategy: {key}")
            elif not isinstance(value, bool):
                raise EstimateError("invalid_inputs", "contours_enabled должен быть true или false")
            params[key] = value
            sources[key] = {"source": "print_scan_strategy", "value": strategy[key]}
    if "hatch_speed_mm_s" in strategy:
        # The explicit job value must not lose to a material's global speed map.
        params["hatch_speeds_by_mat"] = {
            key: value for key, value in (params.get("hatch_speeds_by_mat") or {}).items()
            if key != record["material"]
        }
    origin = (record.get("metadata_json") or {}).get("build_origin_z_mm")
    # Optional configured identities; absent values remain unknown, not inferred
    # from a Monitor100 positional field or a similarly named file.
    for key in ("process_strategy_id", "process_strategy_version", "slicer_version", "firmware_version"):
        value = (record.get("metadata_json") or {}).get(key)
        if isinstance(value, str) and value.strip():
            params[key] = value
    if isinstance(origin, (int, float)) and not isinstance(origin, bool):
        params["build_origin_z_mm"] = float(origin)
    return params, sources


def _geometry_quality(record: dict) -> dict:
    """Operator/importer assessment of how complete the attached geometry is."""
    value = (record.get("metadata_json") or {}).get("geometry_quality") or {}
    return value if isinstance(value, dict) else {}


def _assert_geometry_usable(record: dict) -> None:
    """Refuse a customer-facing estimate for a plate known to be incomplete."""
    quality = _geometry_quality(record)
    if quality.get("status") != "incomplete":
        return
    note = quality.get("note") or "компоновка содержит не все детали или поддержки"
    raise EstimateError(
        "invalid_inputs",
        "Расчёт заблокирован: геометрия карточки помечена как неполная. " + str(note),
    )


def prepare_prediction_inputs(
    repo: PrintsRepository,
    record_id: str,
    *,
    compute_node_id: str | None = None,
) -> PreparedEstimate:
    """Read a compact immutable estimate input snapshot from the database."""
    record = repo.get_print_record(record_id)
    if not record:
        raise EstimateError("not_found", "Карточка печати не найдена")
    require_local_print(record, compute_node_id=compute_node_id)
    _assert_geometry_usable(record)

    files = repo.list_print_files(record_id)
    platform_files = [f for f in files if f["file_type"] in ("stl", "stl_supports")]
    if not platform_files:
        raise EstimateError("invalid_inputs", "К карточке не прикреплён STL")

    material = record["material"]
    params, parameter_sources = _params_with_sources_for_record(repo, record)
    missing = missing_for_estimation(params)
    if missing:
        raise EstimateError(
            "invalid_inputs",
            "Для расчёта не хватает параметров машины: "
            + ", ".join(missing)
            + ". Заполните их в Настройки → Параметры машины.",
        )
    params = effective_params(params)
    from analytics.prediction.layer_engine import scan_geometry_options
    from analytics.prediction.stl_slicer import EstimationError

    try:
        params.update(scan_geometry_options(params))
    except EstimationError as exc:
        raise EstimateError("invalid_inputs", str(exc)) from exc

    # A print card can identify a physical printer through its linked session
    # (or a forward-compatible metadata field before logs are linked).  The
    # current deployment has one machine-parameter row, so None honestly means
    # "the single configured machine", not the operator PC that ran the job.
    printer_id = None
    if record.get("session_id"):
        from domain.models.sessions import BuildSession

        linked_session = repo.db.get(BuildSession, record["session_id"])
        printer_id = linked_session.printer_id if linked_session is not None else None
    if not printer_id:
        candidate = (record.get("metadata_json") or {}).get("printer_id")
        printer_id = str(candidate) if candidate else None

    # Estimation runs after the DB transaction closes and receives only this
    # immutable params copy. Carry physical-machine identity with it so scan
    # and controller-cycle models resolve against NAS-wide machine-scoped keys.
    params = dict(params)
    if printer_id:
        params["printer_id"] = printer_id

    return deepcopy(
        {
            "record": record,
            "platform_files": platform_files,
            "material": material,
            "params": params,
            "parameter_sources": parameter_sources,
            "printer_id": printer_id,
            "powder_cost": record.get("powder_cost_rub_per_kg") or repo.last_powder_cost(),
        }
    )


def geometry_fingerprint(platform_files: list[dict[str, Any]]) -> str:
    """Content identity for leakage-safe model validation across reprints."""
    identities = sorted(
        (
            str(file.get("file_type") or "unknown"),
            str(
                file.get("checksum")
                or f"missing:{file.get('file_name') or ''}:{file.get('size_bytes') or 0}"
            ),
        )
        for file in platform_files
    )
    encoded = json.dumps(
        identities,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "files-sha256:" + hashlib.sha256(encoded).hexdigest()


def prediction_input_hash(prepared: dict[str, Any]) -> str:
    payload = {
        "record_id": prepared["record"]["record_id"],
        "record_revision": prepared["record"]["revision"],
        "material": prepared["material"],
        "params": prepared["params"],
        "powder_cost": prepared["powder_cost"],
        "printer_id": prepared.get("printer_id"),
        "files": [
            {
                "type": f["file_type"],
                "checksum": f["checksum"],
                "uri": f["object_uri"],
            }
            for f in prepared["platform_files"]
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
