"""Print-time estimate for a whole build plate: parts AND supports, one engine.

All bodies are co-hatched per plate layer by ``analytics.prediction.layer_engine``
— real PySLM vectors (hatch + contour + jumps, including jumps between support
walls) on one shared Z axis, integrated over height. The two older, less
accurate paths this replaced are documented in the engine's module docstring.

Scan seconds come from one of two sources, in order:

* **fitted** — a per-(material, layer-thickness) linear model calibrated from
  the machine's own per-layer ``burn_ms`` logs (``scan_model_by_mat``, see
  ``analytics.prediction.scan_calibration``). Absolute: no correction factor
  is stacked on top. Validated on real builds: totals within a few percent
  in-sample; NOT transferable across modes — a model is only ever applied to
  its exact (material, thickness) key.
* **physics** — preset speeds (hatch/contour/jump/support), the cold-start
  path. The per-material blanket correction factor still applies here, as
  before.

Recoat is one pass per plate layer from ``resolve_recoat_ms`` (calibrated →
manual → default), unchanged.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from analytics.prediction.layer_engine import (
    LayerGeometrySeries,
    compute_layer_series,
    resolve_scan_model,
    scan_model_key,
    scan_seconds_from_model,
)
from analytics.prediction.print_time import (
    PrintTimeEstimate,
    resolve_correction_factor,
    resolve_recoat_ms,
)
from analytics.prediction.stl_slicer import EstimationError

logger = logging.getLogger(__name__)

_DEFAULT_JUMP_SPEED_MM_S = 5000.0


@dataclass
class BodyEstimate:
    name: str
    kind: str                   # "part" | "support"
    scan_share: float           # approximate share of the joint scan (by boundary length)
    layer_count: int
    height_mm: float
    volume_cm3: float | None    # None for open shells (volume is meaningless)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PlateEstimate:
    scan_hours: float           # after calibration (physics path) / absolute (fitted)
    recoat_hours: float
    print_hours: float
    total_days: float
    raw_print_hours: float
    correction_factor: float
    layer_count: int            # plate layers (union height)
    height_mm: float
    method: str
    scan_source: str = "physics"          # "fitted" | "physics"
    recoat_time_ms: float = 0.0
    recoat_time_source: str = "default"   # "calibrated" | "manual" | "default"
    parts_volume_mm3: float = 0.0
    bodies: list[BodyEstimate] = field(default_factory=list)
    geometry_series: LayerGeometrySeries | None = None
    warnings: list[str] = field(default_factory=list)

    def as_print_time_estimate(self) -> PrintTimeEstimate:
        """Adapter for consumers of the single-part result (cost estimator)."""
        return PrintTimeEstimate(
            scan_hours=self.scan_hours,
            recoat_hours=self.recoat_hours,
            print_hours=self.print_hours,
            total_days=self.total_days,
            method=self.method,
            raw_print_hours=self.raw_print_hours,
            correction_factor=self.correction_factor,
            breakdown={"recoat_time_ms": round(self.recoat_time_ms, 1),
                      "recoat_time_source": self.recoat_time_source,
                      "scan_source": self.scan_source},
            warnings=list(self.warnings),
        )


def _load_raw_mesh(blob: bytes):
    """Load STL bytes verbatim — no merging, no repair (sheets must survive)."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(blob), file_type="stl", process=False)
    if mesh.is_empty or len(mesh.faces) == 0:
        raise EstimationError("STL не содержит геометрии")
    return mesh


def _physics_scan_seconds(
    totals: dict[str, float], params: dict, material: str, laser_count: int,
    warnings: list[str],
) -> float:
    """Cold-start scan time from preset speeds over the engine's real geometry."""
    by_mat = params.get("hatch_speeds_by_mat") or {}
    hatch_speed = float(by_mat.get(material) or params["hatch_speed_mm_s"])
    contour_speed = float(params.get("contour_speed_mm_s") or hatch_speed)
    support_speed = float(params.get("support_speed_mm_s") or hatch_speed)
    jump_speed = float(params.get("jump_speed_mm_s") or _DEFAULT_JUMP_SPEED_MM_S)
    jump_delay_s = float(params.get("jump_delay_ms") or 0.0) / 1000.0

    if not params.get("jump_speed_mm_s"):
        warnings.append("Скорость перескока не задана — взято значение по умолчанию.")

    seconds = (
        totals["hatch_mm"] / hatch_speed
        + totals["contour_mm"] / contour_speed
        + totals["open_mm"] / support_speed
        + totals["jump_mm"] / jump_speed
        + totals["n_jumps"] * jump_delay_s
    )
    return seconds / max(laser_count, 1)


def estimate_plate(
    parts: list[tuple[str, bytes]],
    supports: list[tuple[str, bytes]],
    params: dict,
    material: str,
) -> PlateEstimate:
    """Estimate machine time for the whole plate.

    ``parts``/``supports`` are ``(display_name, stl_bytes)`` pairs in shared
    plate coordinates (Magics exports satisfy this). Raises ``EstimationError``
    when required machine parameters are missing or no body can be estimated.
    """
    if not parts and not supports:
        raise EstimationError("Не передано ни одной детали и ни одной поддержки")
    thickness = float(params.get("layer_thickness_mm") or 0)
    if thickness <= 0:
        raise EstimationError("Не задана толщина слоя layer_thickness_mm (параметры машины)")
    if not params.get("hatch_speed_mm_s"):
        raise EstimationError("Не задана скорость штриховки (параметры машины)")
    hatch_distance = float(params.get("hatch_distance_mm") or 0)
    if hatch_distance <= 0:
        raise EstimationError("Не задан шаг штриховки hatch_distance_mm (параметры машины)")
    laser_count = int(params.get("laser_count") or 0)
    if laser_count < 1:
        raise EstimationError("Не задано количество лазеров (параметры машины)")

    warnings: list[str] = []
    named = [(name, blob, "part") for name, blob in parts] + [
        (name, blob, "support") for name, blob in supports
    ]
    meshes = []
    metas = []
    parts_volume_mm3 = 0.0
    for name, blob, kind in named:
        mesh = _load_raw_mesh(blob)
        meshes.append(mesh)
        volume = None
        if kind == "part":
            volume = abs(float(mesh.volume))
            parts_volume_mm3 += volume
        metas.append((name, kind, mesh, volume))

    series = compute_layer_series(meshes, hatch_distance, thickness)
    totals = series.totals(thickness)
    plate_layers = series.layer_count(thickness)

    if not supports:
        warnings.append(
            "Поддержки не переданы — реальная печать без поддержек не бывает, "
            "оценка является НИЖНЕЙ границей времени."
        )
    if totals["open_mm"] > 0:
        warnings.append(
            "Часть поддержек — открытые оболочки: их стенки учтены как одиночные "
            "треки, перескоки между ними не моделируются."
        )

    model = resolve_scan_model(params, material, thickness)
    if model is not None:
        raw_scan_hours = scan_seconds_from_model(totals, plate_layers, laser_count, model) / 3600.0
        scan_source = "fitted"
        # The fitted model is absolute (trained on real burn seconds) — stacking
        # the blanket correction factor on top would double-correct.
        factor = 1.0
    else:
        raw_scan_hours = _physics_scan_seconds(totals, params, material, laser_count, warnings) / 3600.0
        scan_source = "physics"
        factor = resolve_correction_factor(params, material)
        warnings.append(
            "Скан рассчитан по паспортным скоростям (нет откалиброванной модели для "
            f"режима {scan_model_key(material, thickness)}) — точность ограничена; "
            "накопите печати с логами и запустите перекалибровку."
        )

    recoat_ms, recoat_source = resolve_recoat_ms(params, material)
    raw_recoat_hours = plate_layers * recoat_ms / 1000.0 / 3600.0
    if recoat_source == "default":
        warnings.append(
            f"Время нанесения слоя не задано и не откалибровано по логам — используется "
            f"значение по умолчанию {recoat_ms / 1000:.1f} с/слой."
        )

    raw_total = raw_scan_hours + raw_recoat_hours

    shares = series.body_shares()
    bodies = [
        BodyEstimate(
            name=name,
            kind=kind,
            scan_share=shares[i] if i < len(shares) else 0.0,
            layer_count=max(int((float(mesh.bounds[1][2]) - float(mesh.bounds[0][2])) / thickness + 0.999999), 1),
            height_mm=float(mesh.bounds[1][2]) - float(mesh.bounds[0][2]),
            volume_cm3=volume / 1000.0 if volume is not None else None,
        )
        for i, (name, kind, mesh, volume) in enumerate(metas)
    ]

    return PlateEstimate(
        scan_hours=raw_scan_hours * factor,
        recoat_hours=raw_recoat_hours * factor,
        print_hours=raw_total * factor,
        total_days=raw_total * factor / 24.0,
        raw_print_hours=raw_total,
        correction_factor=factor,
        layer_count=plate_layers,
        height_mm=series.height_mm,
        method="plate:cohatch" + ("+fitted" if scan_source == "fitted" else ""),
        scan_source=scan_source,
        recoat_time_ms=recoat_ms,
        recoat_time_source=recoat_source,
        parts_volume_mm3=parts_volume_mm3,
        bodies=bodies,
        geometry_series=series,
        warnings=warnings,
    )


__all__ = [
    "PlateEstimate", "BodyEstimate", "estimate_plate",
    "resolve_scan_model", "scan_model_key", "scan_seconds_from_model",
]
