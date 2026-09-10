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
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from analytics.prediction.stl_slicer import EstimationError

logger = logging.getLogger(__name__)

# Uniform sampling grid over plate height (body boundaries are added on top).
_UNIFORM_LEVELS = 90
# Threads used to section levels in parallel. Capped at 4: the measured speedup
# saturates there (2.9x at 4 threads, none beyond). The API container's CPU
# limit was raised from 2.0 to 4.0 to match (docker-compose.yml) — before that
# these 4 threads were fighting over 2 CPU-equivalents. Override with
# PC_SECTION_THREADS if the container's CPU limit changes again.
_SECTION_THREADS = max(1, int(os.environ.get("PC_SECTION_THREADS", "4")))
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
    # Approximate Z intervals in which each input body produced a non-empty
    # sampled section.  Unlike a body's bounding box this does not claim that
    # an arch, disconnected component or internal gap is printed continuously
    # from z_min to z_max. Aligned with the input mesh list.
    body_active_z_intervals_mm: list[list[list[float]]] = field(default_factory=list)
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
        ratio = self.height_mm / layer_thickness_mm
        nearest = round(ratio)
        if math.isclose(ratio, nearest, rel_tol=1e-6, abs_tol=1e-6):
            return max(int(nearest), 1)
        return max(int(math.ceil(ratio)), 1)

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
                integral = (
                    float(series[0]) * max(self.height_mm, layer_thickness_mm)
                    if len(series) else 0.0
                )
            out[name] = integral / layer_thickness_mm
        return out

    def to_snapshot(self) -> dict:
        """Compact JSON form persisted in the prediction snapshot, so later
        calibration can pair stored geometry with real per-layer burn times
        without re-slicing the plate. Also used to persist the geometry cache
        (analytics.prediction.plate_estimator._geometry_cache_key) — includes
        body_boundary_mm so a cache hit still gives correct per-body display
        shares, not just correct aggregate totals."""
        return {
            "zs": [round(z, 3) for z in self.zs],
            **{name: [round(v, 1) for v in getattr(self, name)] for name in GEOMETRY_FEATURES},
            "z_min": round(self.z_min, 3),
            "z_max": round(self.z_max, 3),
            "body_boundary_mm": [round(v, 1) for v in self.body_boundary_mm],
            "body_active_z_intervals_mm": [
                [[round(low, 3), round(high, 3)] for low, high in intervals]
                for intervals in self.body_active_z_intervals_mm
            ],
            "warnings": list(self.warnings),
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
            # Older snapshots (before this field existed) fall back to an empty
            # list, and body_shares() already splits evenly when it's empty —
            # same behaviour as before this field was added.
            body_boundary_mm=list(data.get("body_boundary_mm") or []),
            body_active_z_intervals_mm=list(data.get("body_active_z_intervals_mm") or []),
            warnings=list(data.get("warnings") or []),
        )


def resolve_scan_model(params: dict, material: str, layer_thickness_mm: float) -> dict | None:
    """Fitted scan model for this machine/mode, or a legacy exact-mode model.

    A fitted model must never be applied to a different mode: real-data
    validation showed cross-mode transfer degrades to worse-than-mean (R² < 0).
    """
    models = params.get("scan_model_by_mat") or {}
    keys = []
    printer_id = params.get("printer_id")
    laser_count = int(params.get("laser_count") or 1)
    if printer_id:
        keys.append(machine_mode_key(
            str(printer_id), material, layer_thickness_mm, laser_count,
        ))
    # Backward-compatible single-machine models used this shorter key.
    keys.append(scan_model_key(material, layer_thickness_mm))
    for key in keys:
        model = models.get(key)
        if not isinstance(model, dict):
            continue
        beta = model.get("beta")
        if isinstance(beta, list) and len(beta) == len(GEOMETRY_FEATURES) + 1:
            return model
    return None


def scan_model_key(material: str, layer_thickness_mm: float) -> str:
    return f"{material}@{layer_thickness_mm:.3f}"


def machine_mode_key(
    printer_id: str | None,
    material: str,
    layer_thickness_mm: float,
    laser_count: int,
) -> str:
    """Calibration scope; legacy key when a physical machine is unknown."""
    legacy = scan_model_key(material, layer_thickness_mm)
    if not printer_id:
        return legacy
    return f"{printer_id}|{legacy}|lasers={max(int(laser_count), 1)}"


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


def scan_seconds_by_layer_from_model(
    series: LayerGeometrySeries,
    layer_thickness_mm: float,
    laser_count: int,
    model: dict,
) -> list[float]:
    """Fitted burn prediction at every physical layer centre.

    The aggregate linear scan model can be summed from geometry totals.  A
    minimum machine-cycle floor is nonlinear, however, so downstream cycle
    calculation must retain the distribution over layers and apply ``max``
    before summing.
    """
    beta = model["beta"]
    out: list[float] = []
    for index in range(series.layer_count(layer_thickness_mm)):
        z = series.z_min + (index + 0.5) * layer_thickness_mm
        geometry = series.at(z)
        seconds = sum(
            beta[position] * value
            for position, value in enumerate(geometry)
        ) / max(laser_count, 1)
        seconds += beta[len(GEOMETRY_FEATURES)]
        out.append(max(float(seconds), 0.0))
    return out



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
    """Map a polygon from a section's planar frame into plate XY.

    Polygons from different bodies are only comparable in one shared frame:
    unioning them across mismatched frames merged geometry that is nowhere near
    itself on the real plate, and inter-body jump distances came out measured
    between unrelated origins. ``to_3d`` is the section's 2D→3D transform; the
    section plane is z=const, so applying it and keeping XY lands every body in
    the one plate frame.

    Sectioning through a fixed plane origin/normal (see ``_section_polygons``)
    already yields that shared frame, so in practice this is the identity — it
    stays because the frame is trimesh's to define, not ours to assume.
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
    """Closed shapely polygons (in plate XY) + open track length of one body at z.

    Uses ``section_multiplane`` rather than ``section().to_2D()`` because the
    latter builds a path twice — once from the 3D intersection segments, then
    again after projecting to the plane — and path construction, not the
    intersection itself, dominates the cost on support meshes. Measured 1.9x on
    a 432k-triangle support (176 s → 93 s over 30 levels), 1.23x on a whole
    plate, where Python-side hatching is the rest of the budget. Passing one
    height per call is as fast as batching the whole plate (measured within
    5%), so levels stay independent and parallel.

    It is not bit-identical, and the reason is worth knowing. Building the path
    once instead of twice merges coincident vertices once instead of twice, so
    a handful of support-lattice contours that sit right on the closing
    tolerance land on the other side of it: at five levels of one real plate the
    closed/open split moved, changing the plate totals by at most 0.33% (open
    track length, the bulk of a support, by 0.01%). That is below the ±0.7%
    already contributed by sampling 90 levels instead of every layer, and no
    geometry is discarded — unlike mesh simplification, which was rejected for
    exactly that reason.
    """
    import shapely.geometry

    # A fixed plane origin and normal put every body — and every level — in the
    # same 2D frame, which is what makes inter-body jump distances meaningful.
    paths = mesh.section_multiplane(
        plane_origin=[0.0, 0.0, 0.0], plane_normal=[0.0, 0.0, 1.0], heights=[z],
    )
    path = paths[0] if paths else None
    if path is None:
        return [], 0.0
    total_len = float(path.length)
    polygons: list = []
    closed_perimeter = 0.0
    try:
        to_3d = path.metadata.get("to_3D")
        for poly in path.polygons_full:
            fixed = (_to_plate_xy(poly, to_3d) if to_3d is not None else poly).buffer(_FIX_EPS)
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


def _hatch_level(
    meshes: list, z: float, hatcher,
) -> tuple[float, float, float, float, float, list[float]]:
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
    build_origin_z_mm: float | None = None,
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

    geometry_z_min = min(float(m.bounds[0][2]) for m in meshes)
    z_max = max(float(m.bounds[1][2]) for m in meshes)
    # A raised body does not by itself prove whether native supports extend to
    # Z=0 (real Magics archives contain both coordinate conventions). Use a
    # caller-confirmed origin when available; otherwise the conservative
    # contract begins at the lowest supplied printable geometry and labels the
    # origin as unconfirmed in the prediction snapshot.
    z_min = geometry_z_min if build_origin_z_mm is None else float(build_origin_z_mm)
    if geometry_z_min < z_min - 1e-6:
        raise EstimationError(
            f"Часть STL ниже заданного начала печати Z={z_min:g} мм; "
            "исправьте координаты или build_origin_z_mm"
        )
    if z_max <= z_min:
        raise EstimationError("Нулевая высота компоновки относительно начала печати")
    geometry_warnings: list[str] = []

    pad = layer_thickness_mm / 2.0
    if z_max - z_min <= layer_thickness_mm * (1.0 + 1e-8):
        levels: set[float] = {(z_min + z_max) / 2.0}
    else:
        levels = set(
            float(z) for z in _linspace(z_min + pad, z_max - pad, uniform_levels)
        )
    # Body boundaries: the geometry changes discontinuously where a body starts
    # or ends, so sample just inside each boundary.
    for mesh in meshes:
        for zb in (float(mesh.bounds[0][2]) + pad, float(mesh.bounds[1][2]) - pad):
            if z_min < zb < z_max:
                levels.add(zb)

    zs = sorted(levels)
    # Levels are independent — nothing carries over from one z to the next — so
    # they run in parallel. Threads rather than processes: the expensive part is
    # trimesh's mesh sectioning, which drops the GIL inside its C code, and the
    # meshes would have to be pickled to every process otherwise. Measured on a
    # 432k-triangle support: 38.2 s sequential, 13.1 s across 4 threads (2.9x),
    # with no further gain at 8 — the Python-side hatching is the remaining
    # serial part.
    #
    # Each level needs its own Hatcher: PySLM's hatcher carries mutable state
    # between calls, so sharing one across threads would corrupt results.
    results = [None] * len(zs)
    if len(zs) > 1 and _SECTION_THREADS > 1:
        with ThreadPoolExecutor(max_workers=_SECTION_THREADS) as pool:
            futures = {
                pool.submit(_hatch_level, meshes, z, _make_hatcher(hatch_distance_mm)): i
                for i, z in enumerate(zs)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    else:
        hatcher = _make_hatcher(hatch_distance_mm)
        results = [_hatch_level(meshes, z, hatcher) for z in zs]

    columns = {name: [] for name in GEOMETRY_FEATURES}
    body_totals = [0.0] * len(meshes)
    body_activity = [[] for _ in meshes]
    for h, c, j, nj, o, per_body in results:
        columns["hatch_mm"].append(h)
        columns["contour_mm"].append(c)
        columns["jump_mm"].append(j)
        columns["n_jumps"].append(nj)
        columns["open_mm"].append(o)
        for i, v in enumerate(per_body):
            body_totals[i] += v
            body_activity[i].append(v > _FIX_EPS)

    # Convert the sampled activity mask to compact Z intervals. Boundaries are
    # halfway to the neighbouring sample and therefore remain explicitly
    # approximate; body bounds clip them more tightly in plate_estimator.
    body_intervals: list[list[list[float]]] = []
    for activity in body_activity:
        intervals: list[list[float]] = []
        start_index: int | None = None
        for index, active in enumerate([*activity, False]):
            if active and start_index is None:
                start_index = index
            elif not active and start_index is not None:
                end_index = index - 1
                low = (
                    z_min if start_index == 0
                    else (zs[start_index - 1] + zs[start_index]) / 2.0
                )
                high = (
                    z_max if end_index == len(zs) - 1
                    else (zs[end_index] + zs[end_index + 1]) / 2.0
                )
                intervals.append([float(low), float(high)])
                start_index = None
        body_intervals.append(intervals)

    series = LayerGeometrySeries(
        zs=zs,
        z_min=z_min,
        z_max=z_max,
        body_boundary_mm=body_totals,
        body_active_z_intervals_mm=body_intervals,
        **columns,
        warnings=geometry_warnings,
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
