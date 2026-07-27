"""Print-time estimate for a whole build plate: parts AND supports.

Why this exists: the per-part PySLM path assumes watertight solids. Real
Magics support exports are open sheet meshes; feeding them into that path made
MeshFix silently discard geometry (on one real build more than half the plate
height vanished and the estimate came out ~5x low while looking confident).
The correct estimate needs two different physical models:

* **Parts** (watertight solids) — PySLM vector trajectories, as before:
  hatch + contour + jumps, sampled over layers (``estimate_print_time``).
* **Supports** (open shells / thin walls) — per-layer cross-section geometry:
  open curve length is scanned as single tracks, closed regions as
  area/hatch_distance fill plus contour. No repair is ever attempted.

Recoating is one pass per plate layer regardless of how many bodies exist, so
it is computed from the union height. The per-material calibration factor is
applied once to the combined total (same closed-loop as the single-part path).

Honesty rules encoded here:
* an estimate with zero supports carries an explicit warning — real builds
  always have them, so the number is a lower bound;
* support jump travel between walls is not modelled (no vectors) — stated in
  the output rather than silently ignored;
* any body that fails its model degrades with a named warning, never silently.
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field

from analytics.prediction.print_time import (
    PrintTimeEstimate,
    estimate_print_time,
    resolve_correction_factor,
    resolve_recoat_ms,
)
from analytics.prediction.stl_slicer import EstimationError, SliceResult, slice_stl

logger = logging.getLogger(__name__)

# Cross-sections sampled per body for the section-based model. Fewer for very
# heavy meshes: each section costs O(faces).
_SECTIONS_PER_BODY = 60
_SECTIONS_HEAVY = 30
_HEAVY_FACES = 200_000


@dataclass
class BodyEstimate:
    name: str
    kind: str                   # "part" | "support"
    method: str                 # "pyslm" | "sections"
    raw_scan_hours: float       # laser-divided, BEFORE calibration
    layer_count: int
    height_mm: float
    volume_cm3: float | None    # None for open shells (volume is meaningless)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PlateEstimate:
    scan_hours: float           # after calibration
    recoat_hours: float
    print_hours: float
    total_days: float
    raw_print_hours: float
    correction_factor: float
    layer_count: int            # plate layers (union height)
    height_mm: float
    method: str
    recoat_time_ms: float = 0.0
    recoat_time_source: str = "default"  # "calibrated" | "manual" | "default"
    bodies: list[BodyEstimate] = field(default_factory=list)
    part_slices: list[SliceResult] = field(default_factory=list)  # for cost reuse
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
                      "recoat_time_source": self.recoat_time_source},
            warnings=list(self.warnings),
        )


def _load_raw_mesh(blob: bytes):
    """Load STL bytes verbatim — no merging, no repair (sheets must survive)."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(blob), file_type="stl", process=False)
    if mesh.is_empty or len(mesh.faces) == 0:
        raise EstimationError("STL не содержит геометрии")
    return mesh


def _section_geometry(mesh, z: float) -> tuple[float, float, float]:
    """(closed_area_mm2, closed_perimeter_mm, open_length_mm) at height z."""
    section = mesh.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
    if section is None:
        return 0.0, 0.0, 0.0
    total_len = float(section.length)
    try:
        # trimesh renamed to_planar -> to_2D; support both (CI pins 4.12).
        to_2d = getattr(section, "to_2D", None) or section.to_planar
        planar, _ = to_2d()
        polys = planar.polygons_full
        area = float(sum(p.area for p in polys))
        perimeter = float(
            sum(p.exterior.length + sum(r.length for r in p.interiors) for p in polys)
        )
    except Exception:
        # Open curves only — no closed regions recoverable.
        area, perimeter = 0.0, 0.0
    open_len = max(0.0, total_len - perimeter)
    return area, perimeter, open_len


def _section_scan_hours(mesh, params: dict, material: str, laser_count: int) -> tuple[float, int, float]:
    """Section-model scan time for one body.

    Returns (raw laser-divided hours, body layer count, body height). Open
    curves are single laser tracks; closed regions are hatch fill + contour.
    """
    thickness = float(params["layer_thickness_mm"])
    by_mat = params.get("hatch_speeds_by_mat") or {}
    hatch_speed = float(by_mat.get(material) or params["hatch_speed_mm_s"])
    hatch_distance = float(params["hatch_distance_mm"])
    contour_speed = float(params.get("contour_speed_mm_s") or hatch_speed)
    support_speed = float(params.get("support_speed_mm_s") or hatch_speed)

    z_min, z_max = float(mesh.bounds[0][2]), float(mesh.bounds[1][2])
    height = max(z_max - z_min, 0.0)
    layers = max(int(math.ceil(height / thickness)), 1)

    n = min(_SECTIONS_HEAVY if len(mesh.faces) > _HEAVY_FACES else _SECTIONS_PER_BODY, layers)
    step = height / n if n else thickness
    per_layer_seconds = []
    for i in range(n):
        z = z_min + (i + 0.5) * step
        area, perimeter, open_len = _section_geometry(mesh, z)
        seconds = (
            (area / hatch_distance) / hatch_speed
            + perimeter / contour_speed
            + open_len / support_speed
        )
        per_layer_seconds.append(seconds)

    mean_s = sum(per_layer_seconds) / len(per_layer_seconds) if per_layer_seconds else 0.0
    return (mean_s * layers / max(laser_count, 1)) / 3600.0, layers, height


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
    if not params.get("hatch_distance_mm"):
        raise EstimationError("Не задан шаг штриховки hatch_distance_mm (параметры машины)")
    laser_count = int(params.get("laser_count") or 0)
    if laser_count < 1:
        raise EstimationError("Не задано количество лазеров (параметры машины)")

    warnings: list[str] = []
    bodies: list[BodyEstimate] = []
    part_slices: list[SliceResult] = []
    z_lo, z_hi = math.inf, -math.inf

    for name, blob in parts:
        body_warn: list[str] = []
        mesh = _load_raw_mesh(blob)
        z_lo = min(z_lo, float(mesh.bounds[0][2]))
        z_hi = max(z_hi, float(mesh.bounds[1][2]))
        try:
            slices = slice_stl(blob, thickness)
            est = estimate_print_time(slices, params, material, stl_bytes=blob)
            raw_scan = est.scan_hours / est.correction_factor if est.correction_factor else est.scan_hours
            part_slices.append(slices)
            bodies.append(BodyEstimate(
                name=name, kind="part", method="pyslm",
                raw_scan_hours=raw_scan,
                layer_count=slices.layer_count,
                height_mm=slices.height_mm,
                volume_cm3=slices.volume_mm3 / 1000.0,
                warnings=body_warn,
            ))
        except EstimationError as exc:
            # Named, visible degradation — never a silent one.
            body_warn.append(
                f"«{name}»: точный расчёт PySLM недоступен ({exc}) — использована модель по сечениям."
            )
            raw_scan, layers, height = _section_scan_hours(mesh, params, material, laser_count)
            bodies.append(BodyEstimate(
                name=name, kind="part", method="sections",
                raw_scan_hours=raw_scan, layer_count=layers, height_mm=height,
                volume_cm3=None, warnings=body_warn,
            ))
        warnings.extend(body_warn)

    for name, blob in supports:
        mesh = _load_raw_mesh(blob)
        z_lo = min(z_lo, float(mesh.bounds[0][2]))
        z_hi = max(z_hi, float(mesh.bounds[1][2]))
        raw_scan, layers, height = _section_scan_hours(mesh, params, material, laser_count)
        bodies.append(BodyEstimate(
            name=name, kind="support", method="sections",
            raw_scan_hours=raw_scan, layer_count=layers, height_mm=height,
            volume_cm3=None,
        ))

    if not supports:
        warnings.append(
            "Поддержки не переданы — реальная печать без поддержек не бывает, "
            "оценка является НИЖНЕЙ границей времени."
        )
    else:
        warnings.append(
            "Перескоки лазера между стенками поддержек не моделируются "
            "(нет векторов сканирования) — время поддержек слегка занижено."
        )

    plate_height = max(z_hi - z_lo, 0.0)
    plate_layers = max(int(math.ceil(plate_height / thickness)), 1)

    recoat_ms, recoat_source = resolve_recoat_ms(params, material)
    raw_recoat = plate_layers * recoat_ms / 1000.0 / 3600.0
    if recoat_source == "default":
        warnings.append(
            f"Время нанесения слоя не задано и не откалибровано по логам — используется "
            f"значение по умолчанию {recoat_ms / 1000:.1f} с/слой. На тонких слоях это "
            "доминирующая часть времени: задайте его в параметрах машины или накопите "
            "историю печатей для автокалибровки."
        )

    raw_scan_total = sum(b.raw_scan_hours for b in bodies)
    raw_total = raw_scan_total + raw_recoat
    factor = resolve_correction_factor(params, material)

    return PlateEstimate(
        scan_hours=raw_scan_total * factor,
        recoat_hours=raw_recoat * factor,
        print_hours=raw_total * factor,
        total_days=raw_total * factor / 24.0,
        raw_print_hours=raw_total,
        correction_factor=factor,
        layer_count=plate_layers,
        height_mm=plate_height,
        method="plate:pyslm+sections",
        recoat_time_ms=recoat_ms,
        recoat_time_source=recoat_source,
        bodies=bodies,
        part_slices=part_slices,
        warnings=warnings,
    )


__all__ = ["PlateEstimate", "BodyEstimate", "estimate_plate"]
