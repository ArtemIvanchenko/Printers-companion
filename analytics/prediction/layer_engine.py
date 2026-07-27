"""Per-plate-layer scan geometry: every body co-hatched on one shared Z axis.

This replaces two less accurate paths that previously coexisted:

* ``print_time._pyslm_layer_metrics`` hatched 10 sampled sections of ONE body
  and multiplied the *mean* by the layer count — geometry variation with height
  was averaged away (on a real plate the hatch length varies ~10x between the
  support-dense bottom and the top).
* ``plate_estimator._section_scan_hours`` timed supports as bare single tracks
  with no jumps at all — on a real build the laser-off travel between support
  walls exceeded the hatch length itself (measured: 16.9 m of jumps vs 10.3 m
  of hatch on one layer, ~1000-1600 jumps/layer).

Here every body's cross-section at a given z — part solids and support walls
alike — is fed into ONE PySLM ``Hatcher.hatch()`` call as boundary paths
(``Hatcher.hatch`` accepts ``List[np.ndarray]``, not only whole STL parts), so
hatch, contour and inter-body jump geometry come from the same real vector
pass the machine itself would make. Open (sheet) sections that cannot form
closed polygons are carried as single-track length.

The output is a sampled series over plate height. Sample levels include every
body's z-boundaries — that is where the geometry jumps — plus a uniform grid;
totals integrate the series (trapezoid) instead of scaling a mean.

Measured cost: a real 72-body, 119 mm plate at ~108 levels ≈ 2.5 min. This runs
in the background auto-estimate path, same budget as the code it replaces.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from analytics.prediction.stl_slicer import EstimationError

logger = logging.getLogger(__name__)

# Uniform sampling grid over plate height (body boundaries are added on top).
_UNIFORM_LEVELS = 90
# PySLM polygon fix epsilon, mirrors pyslm.core.Part.POLYGON_FIX_EPSILON.
_FIX_EPS = 0.001

# Geometry component order — shared contract with the fitted-scan-model storage
# (machine_params.scan_model_by_mat) and the calibration fit. Do not reorder:
# stored beta vectors are positional.
GEOMETRY_FEATURES = ("hatch_mm", "contour_mm", "jump_mm", "n_jumps", "open_mm")


@dataclass
class LayerGeometrySeries:
    """Sampled per-layer scan geometry of a whole plate."""

    zs: list[float]                       # sample heights, ascending (plate coords)
    hatch_mm: list[float]
    contour_mm: list[float]
    jump_mm: list[float]
    n_jumps: list[float]
    open_mm: list[float]
    z_min: float
    z_max: float
    # Per input body: total boundary length across all sampled levels. The scan
    # TIME is joint (bodies are co-hatched); this is only for attributing a
    # display share per body. Aligned with the meshes list passed in.
    body_boundary_mm: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def body_shares(self) -> list[float]:
        total = sum(self.body_boundary_mm)
        if total <= 0:
            n = len(self.body_boundary_mm)
            return [1.0 / n] * n if n else []
        return [v / total for v in self.body_boundary_mm]

    @property
    def height_mm(self) -> float:
        return max(self.z_max - self.z_min, 0.0)

    def layer_count(self, layer_thickness_mm: float) -> int:
        return max(int(math.ceil(self.height_mm / layer_thickness_mm)), 1)

    def at(self, z: float) -> tuple[float, ...]:
        """Geometry components at height z (linear interpolation between samples)."""
        import numpy as np

        return tuple(
            float(np.interp(z, self.zs, getattr(self, name))) for name in GEOMETRY_FEATURES
        )

    def totals(self, layer_thickness_mm: float) -> dict[str, float]:
        """Integrated per-plate totals: trapezoid over z, divided by thickness.

        Equivalent to summing every physical layer's geometry, without assuming
        the cross-section is constant between samples.
        """
        import numpy as np

        trapezoid = getattr(np, "trapezoid", None) or np.trapz  # numpy<2 fallback
        zs = np.asarray(self.zs)
        out: dict[str, float] = {}
        for name in GEOMETRY_FEATURES:
            series = np.asarray(getattr(self, name))
            if len(zs) >= 2:
                integral = float(trapezoid(series, zs))
                # Edge half-layers outside the first/last sample midpoints.
                integral += float(series[0]) * (zs[0] - self.z_min)
                integral += float(series[-1]) * (self.z_max - zs[-1])
            else:
                integral = float(series[0]) * self.height_mm if len(series) else 0.0
            out[name] = integral / layer_thickness_mm
        return out

    def to_snapshot(self) -> dict:
        """Compact JSON form persisted in the prediction snapshot, so later
        calibration can pair stored geometry with real per-layer burn times
        without re-slicing the plate."""
        return {
            "zs": [round(z, 3) for z in self.zs],
            **{name: [round(v, 1) for v in getattr(self, name)] for name in GEOMETRY_FEATURES},
            "z_min": round(self.z_min, 3),
            "z_max": round(self.z_max, 3),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "LayerGeometrySeries":
        return cls(
            zs=list(data["zs"]),
            hatch_mm=list(data["hatch_mm"]),
            contour_mm=list(data["contour_mm"]),
            jump_mm=list(data["jump_mm"]),
            n_jumps=list(data["n_jumps"]),
            open_mm=list(data["open_mm"]),
            z_min=float(data["z_min"]),
            z_max=float(data["z_max"]),
        )


def resolve_scan_model(params: dict, material: str, layer_thickness_mm: float) -> dict | None:
    """Fitted scan model for exactly this (material, thickness), or None.

    A fitted model must never be applied to a different mode: real-data
    validation showed cross-mode transfer degrades to worse-than-mean (R² < 0).
    """
    models = params.get("scan_model_by_mat") or {}
    model = models.get(scan_model_key(material, layer_thickness_mm))
    if not isinstance(model, dict):
        return None
    beta = model.get("beta")
    if not isinstance(beta, list) or len(beta) != len(GEOMETRY_FEATURES) + 1:
        return None
    return model


def scan_model_key(material: str, layer_thickness_mm: float) -> str:
    return f"{material}@{layer_thickness_mm:.3f}"


def scan_seconds_from_model(
    totals: dict[str, float], layer_count: int, laser_count: int, model: dict,
) -> float:
    """Total scan seconds from a fitted per-layer linear model.

    Per-layer model: seconds = Σ beta_k * g_k / lasers + intercept; summed over
    layers the geometry sums to plate totals and the intercept scales by the
    layer count.
    """
    beta = model["beta"]
    seconds = sum(
        beta[k] * totals[name] for k, name in enumerate(GEOMETRY_FEATURES)
    ) / max(laser_count, 1)
    seconds += beta[len(GEOMETRY_FEATURES)] * layer_count
    return seconds



def _make_hatcher(hatch_distance_mm: float):
    from pyslm import hatching as slm_hatching

    hatcher = slm_hatching.Hatcher()
    hatcher.hatchDistance = hatch_distance_mm
    hatcher.hatchAngle = 67.0
    hatcher.volumeOffsetHatch = 0.08
    hatcher.spotCompensation = 0.06
    hatcher.numInnerContours = 1
    hatcher.numOuterContours = 1
    return hatcher


def _to_plate_xy(polygon, to_3d) -> "shapely.geometry.Polygon":  # noqa: F821
    """Map a polygon from trimesh's per-section planar frame into plate XY.

    ``to_2D()`` gives every body its OWN 2D frame (arbitrary in-plane rotation
    and origin). Polygons from different bodies are not comparable in those
    frames: unioning them merged geometry that is nowhere near itself on the
    real plate, and inter-body jump distances were measured between unrelated
    frames. ``to_3d`` is the section's 2D→3D transform; the section plane is
    z=const, so applying it and keeping XY lands every body in the one shared
    plate frame.
    """
    import numpy as np
    import shapely.geometry

    def ring(coords):
        pts = np.asarray(coords, dtype=float)
        hom = np.column_stack([pts[:, 0], pts[:, 1], np.zeros(len(pts)), np.ones(len(pts))])
        world = (np.asarray(to_3d) @ hom.T).T
        return world[:, :2]

    return shapely.geometry.Polygon(
        ring(polygon.exterior.coords),
        [ring(r.coords) for r in polygon.interiors],
    )


def _section_polygons(mesh, z: float):
    """Closed shapely polygons (in plate XY) + open track length of one body at z."""
    import shapely.geometry

    section = mesh.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
    if section is None:
        return [], 0.0
    total_len = float(section.length)
    polygons: list = []
    closed_perimeter = 0.0
    try:
        to_2d = getattr(section, "to_2D", None) or section.to_planar
        planar, to_3d = to_2d()
        for poly in planar.polygons_full:
            fixed = _to_plate_xy(poly, to_3d).buffer(_FIX_EPS)
            geoms = fixed.geoms if isinstance(fixed, shapely.geometry.MultiPolygon) else [fixed]
            for g in geoms:
                closed_perimeter += g.exterior.length + sum(r.length for r in g.interiors)
                polygons.append(g)
    except Exception:
        # Open curves only (sheet supports) — nothing closed recoverable.
        pass
    open_len = max(0.0, total_len - closed_perimeter)
    return polygons, open_len


def _polygons_to_paths(polygons: list) -> list:
    """Union overlapping regions, then emit hatcher boundary paths.

    The union is physical, not cosmetic: Magics supports intentionally
    penetrate the part by a fraction of a millimetre, and coincident/overlapping
    boundaries fed to the hatcher cancel even-odd — the overlap region would
    silently lose its hatching. Powder in an overlap is melted once; the union
    says exactly that.
    """
    import numpy as np
    import shapely.geometry
    from shapely.ops import unary_union

    if not polygons:
        return []
    merged = unary_union(polygons)
    geoms = merged.geoms if isinstance(merged, shapely.geometry.MultiPolygon) else [merged]
    paths: list = []
    for g in geoms:
        if g.is_empty or not isinstance(g, shapely.geometry.Polygon):
            continue
        paths.append(np.array(g.exterior.coords.xy).T)
        paths.extend(np.array(r.coords.xy).T for r in g.interiors)
    return paths


def _hatch_level(meshes: list, z: float, hatcher) -> tuple[float, float, float, float, float]:
    """Co-hatch every body's closed sections at z; returns geometry components."""
    import numpy as np
    import pyslm
    import pyslm.analysis

    all_polygons: list = []
    open_len = 0.0
    body_boundary = []
    for mesh in meshes:
        polygons, body_open = _section_polygons(mesh, z)
        all_polygons.extend(polygons)
        open_len += body_open
        body_boundary.append(
            sum(p.exterior.length + sum(r.length for r in p.interiors) for p in polygons)
            + body_open
        )
    all_paths = _polygons_to_paths(all_polygons)

    hatch_len = contour_len = jump_len = 0.0
    n_jumps = 0
    if all_paths:
        layer = hatcher.hatch(all_paths)
        if layer:
            for geom in layer.geometry:
                length = pyslm.analysis.getLayerGeometryPathLength(geom)
                if isinstance(geom, pyslm.geometry.ContourGeometry):
                    contour_len += length
                else:
                    hatch_len += length
                    coords = np.asarray(geom.coords)
                    if len(coords) >= 4:
                        ends, starts = coords[1::2], coords[2::2]
                        k = min(len(ends) - 1, len(starts))
                        if k > 0:
                            jump_len += float(np.linalg.norm(ends[:k] - starts[:k], axis=1).sum())
                            n_jumps += k
    return hatch_len, contour_len, jump_len, float(n_jumps), open_len, body_boundary


def compute_layer_series(
    meshes: list,
    hatch_distance_mm: float,
    layer_thickness_mm: float,
    uniform_levels: int = _UNIFORM_LEVELS,
) -> LayerGeometrySeries:
    """Co-hatched geometry series for a plate of trimesh bodies (shared coords).

    Raises EstimationError when PySLM is unavailable or no geometry survives —
    never silently degrades to a coarser model.
    """
    try:
        import numpy as np  # noqa: F401
        import pyslm  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise EstimationError(f"PySLM недоступен — точный расчёт невозможен ({exc})")
    if not meshes:
        raise EstimationError("Не передано ни одного тела")

    z_min = min(float(m.bounds[0][2]) for m in meshes)
    z_max = max(float(m.bounds[1][2]) for m in meshes)
    if z_max - z_min <= 0:
        raise EstimationError("Нулевая высота компоновки")

    pad = layer_thickness_mm / 2.0
    levels: set[float] = set(
        float(z) for z in _linspace(z_min + pad, z_max - pad, uniform_levels)
    )
    # Body boundaries: the geometry changes discontinuously where a body starts
    # or ends, so sample just inside each boundary.
    for mesh in meshes:
        for zb in (float(mesh.bounds[0][2]) + pad, float(mesh.bounds[1][2]) - pad):
            if z_min < zb < z_max:
                levels.add(zb)

    hatcher = _make_hatcher(hatch_distance_mm)
    zs = sorted(levels)
    columns = {name: [] for name in GEOMETRY_FEATURES}
    body_totals = [0.0] * len(meshes)
    for z in zs:
        h, c, j, nj, o, per_body = _hatch_level(meshes, z, hatcher)
        columns["hatch_mm"].append(h)
        columns["contour_mm"].append(c)
        columns["jump_mm"].append(j)
        columns["n_jumps"].append(nj)
        columns["open_mm"].append(o)
        for i, v in enumerate(per_body):
            body_totals[i] += v

    series = LayerGeometrySeries(
        zs=zs, z_min=z_min, z_max=z_max, body_boundary_mm=body_totals, **columns,
    )
    if sum(series.hatch_mm) + sum(series.open_mm) <= 0:
        raise EstimationError(
            "Ни на одном уровне не получено сканируемой геометрии — проверьте файлы"
        )
    return series


def _linspace(start: float, stop: float, n: int) -> list[float]:
    if n <= 1 or stop <= start:
        return [start]
    step = (stop - start) / (n - 1)
    return [start + i * step for i in range(n)]


__all__ = [
    "LayerGeometrySeries", "compute_layer_series", "GEOMETRY_FEATURES",
    "resolve_scan_model", "scan_model_key", "scan_seconds_from_model",
]
