"""Local section geometry and scan/cycle math, without SQL or storage IO.

The existing plate path co-hatches closed polygons on a shared Z axis and
carries open sections as track length. This is a geometric proxy, NOT proof of
native slicer vectors, phase activation, support travel or laser scheduling.
Its sampled grid includes body boundaries; totals integrate that series.

The separate section-track helper retains all Line entities and their stated
STL-derived order at exact heights. Callers must supply phase recipes and keep
unverified trajectory/activation hypotheses out of production calibration.
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
DEFAULT_JUMP_SPEED_MM_S = 5000.0
DEFAULT_HATCH_ANGLE_DEG = 67.0

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

    def at_heights(self, zs):
        """Batch interpolation at caller-provided heights, preserving their order."""
        import numpy as np

        return np.column_stack([
            np.interp(zs, self.zs, getattr(self, name)) for name in GEOMETRY_FEATURES
        ])

    def at_layers(self, layer_thickness_mm: float):
        """Geometry at physical layer centres, in GEOMETRY_FEATURES order.

        Interpolate each component once for all layers, rather than repeatedly
        converting the sampled lists for every scalar interpolation.
        """
        import numpy as np

        zs = self.z_min + (
            np.arange(self.layer_count(layer_thickness_mm)) + 0.5
        ) * layer_thickness_mm
        return self.at_heights(zs)

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
    """Only reuse an artifact fitted to these captured machine/scan inputs.

    Legacy artifacts stay readable as history, but do not authorise reuse.
    Matching configured inputs is not confirmation of the actual as-run recipe.
    """
    from analytics.prediction.scan_scope import scan_scope, scan_scope_key

    scope = scan_scope(params, material, layer_thickness_mm)
    if scope is None:
        return None
    models = params.get("scan_model_by_mat") or {}
    model = models.get(scan_scope_key(scope))
    if not isinstance(model, dict) or model.get("scan_calibration_scope") != scope:
        return None
    beta = model.get("beta")
    if (isinstance(beta, list) and len(beta) == len(GEOMETRY_FEATURES) + 1
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0 for value in beta)):
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
    import numpy as np

    beta = model["beta"]
    geometry = series.at_layers(layer_thickness_mm)
    seconds = (geometry * beta[:len(GEOMETRY_FEATURES)]).sum(axis=1) / max(laser_count, 1)
    seconds += beta[len(GEOMETRY_FEATURES)]
    return np.maximum(seconds, 0.0).tolist()


def physics_scan_seconds_by_layer(
    series: LayerGeometrySeries,
    layer_thickness_mm: float,
    params: dict,
    material: str,
    laser_count: int,
    *, heights=None,
) -> list[float]:
    """Cold-start physics burn at layer centres or exact diagnostic heights.

    Explicit heights retain their order and gaps; they never change the datum
    or supply geometry/measurements for missing layers.
    """
    by_mat = params.get("hatch_speeds_by_mat") or {}
    hatch_speed = float(by_mat.get(material) or params["hatch_speed_mm_s"])
    contour_speed = float(params.get("contour_speed_mm_s") or hatch_speed)
    support_speed = float(params.get("support_speed_mm_s") or hatch_speed)
    jump_speed = float(params.get("jump_speed_mm_s") or DEFAULT_JUMP_SPEED_MM_S)
    jump_delay_s = float(params.get("jump_delay_ms") or 0.0) / 1000.0
    geometry = (series.at_layers(layer_thickness_mm) if heights is None
                else series.at_heights(heights))
    hatch_mm, contour_mm, jump_mm, n_jumps, open_mm = geometry.T
    return ((
        hatch_mm / hatch_speed
        + contour_mm / contour_speed
        + open_mm / support_speed
        + jump_mm / jump_speed
        + n_jumps * jump_delay_s
    ) / max(laser_count, 1)).tolist()


def machine_cycle_from_layers(
    raw_scan_seconds_by_layer: list[float],
    *,
    scan_correction_factor: float,
    recoat_ms: float,
    cycle_model: dict | None,
) -> tuple[float, float, int]:
    """Return full normal cycle seconds, controller overhead and floor hits.

    The scan correction is applied *before* the nonlinear floor.  Summing scan
    first would be wrong: two builds can have the same total scan time but a
    different number of short layers held at the controller's minimum cycle.
    """
    recoat_seconds = recoat_ms / 1000.0
    base_seconds = (
        float(cycle_model.get("base_overhead_ms") or 0.0) / 1000.0
        if cycle_model else 0.0
    )
    floor_seconds = (
        float(cycle_model["minimum_cycle_ms"]) / 1000.0
        if cycle_model and cycle_model.get("minimum_cycle_ms") is not None else None
    )
    total_seconds = 0.0
    overhead_seconds = 0.0
    floor_active_layers = 0
    for raw_scan_seconds in raw_scan_seconds_by_layer:
        scan_seconds = raw_scan_seconds * scan_correction_factor
        base_cycle = scan_seconds + recoat_seconds + base_seconds
        if floor_seconds is not None and floor_seconds > base_cycle:
            cycle_seconds = floor_seconds
            floor_active_layers += 1
        else:
            cycle_seconds = base_cycle
        total_seconds += cycle_seconds
        overhead_seconds += cycle_seconds - scan_seconds - recoat_seconds
    return total_seconds, max(overhead_seconds, 0.0), floor_active_layers


def _clip_hatch_lines(paths, lines):
    """Cull impossible segment/box intersections before the unchanged clipper.

    The three separating axes are X, Y and the segment normal. Boxes include
    two clipping-grid units: rounding either endpoint or boundary must not
    turn a possible intersection into a rejected line. Kept coordinates and
    pseudo-Z ordering IDs are passed through in their original order.
    """
    import numpy as np
    from pyslm.hatching import BaseHatcher

    vectors = lines.reshape(-1, 2, 3)
    if not np.isfinite(vectors).all():
        raise EstimationError("Неконечные координаты штриховки")
    # PySLM generates float32 endpoints. Promote only the rejection arithmetic
    # so its rounding cannot consume the clipping-grid margin; retain the
    # original coordinates for the actual intersection.
    xy = vectors[:, :, :2].astype(np.float64, copy=False)
    starts, ends = xy[:, 0], xy[:, 1]
    vector_low, vector_high = np.minimum(starts, ends), np.maximum(starts, ends)
    delta = ends - starts
    keep = np.zeros(len(vectors), dtype=bool)
    padding = 2 * BaseHatcher.error()
    for path in paths:
        coords = np.asarray(path)[:, :2]
        if not np.isfinite(coords).all():
            raise EstimationError("Неконечные координаты контура")
        low, high = coords.min(axis=0) - padding, coords.max(axis=0) + padding
        middle, half = (low + high) / 2, (high - low) / 2
        offset = middle - starts
        distance = np.abs(offset[:, 0] * delta[:, 1] - offset[:, 1] * delta[:, 0])
        radius = half[0] * np.abs(delta[:, 1]) + half[1] * np.abs(delta[:, 0])
        keep |= (
            (vector_low[:, 0] <= high[0]) & (vector_high[:, 0] >= low[0])
            & (vector_low[:, 1] <= high[1]) & (vector_high[:, 1] >= low[1])
            & (distance <= radius)
        )
    clipped = BaseHatcher.clipLines(paths, vectors[keep].reshape(-1, 3))
    # Upstream may represent no intersections as (1, 0, 2). Hatcher tests len,
    # then indexes the absent endpoints; a real empty result avoids that crash.
    return clipped if clipped.size else np.empty((0, 2, 3))


def scan_geometry_options(params: dict) -> dict:
    """The supported geometric scan inputs, with strict, reproducible defaults.

    These two options do not describe a complete slicer recipe: stripes,
    per-layer rotation, skin regions and per-model laser assignment remain
    separate evidence. Never interpret a string such as "false" as a flag.
    """
    enabled = params.get("contours_enabled", True)
    angle = params.get("hatch_angle_deg", DEFAULT_HATCH_ANGLE_DEG)
    if not isinstance(enabled, bool):
        raise EstimationError("contours_enabled должен быть true или false")
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        raise EstimationError("hatch_angle_deg должен быть конечным числом")
    try:
        angle = float(angle)
    except OverflowError as exc:
        raise EstimationError("hatch_angle_deg должен быть конечным числом") from exc
    if not math.isfinite(angle):
        raise EstimationError("hatch_angle_deg должен быть конечным числом")
    return {"contours_enabled": enabled, "hatch_angle_deg": angle}


def _make_hatcher(
    hatch_distance_mm: float, *, contours_enabled: bool = True,
    hatch_angle_deg: float = DEFAULT_HATCH_ANGLE_DEG,
):
    from pyslm import hatching as slm_hatching

    class BoundedHatcher(slm_hatching.Hatcher):
        clipLines = staticmethod(_clip_hatch_lines)

    hatcher = BoundedHatcher()
    hatcher.hatchDistance = hatch_distance_mm
    hatcher.hatchAngle = hatch_angle_deg
    hatcher.volumeOffsetHatch = 0.08
    hatcher.spotCompensation = 0.06
    hatcher.numInnerContours = int(contours_enabled)
    hatcher.numOuterContours = int(contours_enabled)
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
    usually yields that shared frame. Skip rebuilding the polygon only when
    the recorded XY transform is exactly identity; never infer it from the
    plane normal or apply a tolerance to real offsets/rotations.
    """
    import numpy as np
    import shapely.geometry

    transform = np.asarray(to_3d)
    if np.array_equal(transform[:2], [[1, 0, 0, 0], [0, 1, 0, 0]]):
        return polygon

    def ring(coords):
        pts = np.asarray(coords, dtype=float)
        hom = np.column_stack([pts[:, 0], pts[:, 1], np.zeros(len(pts)), np.ones(len(pts))])
        world = (transform @ hom.T).T
        return world[:, :2]

    return shapely.geometry.Polygon(
        ring(polygon.exterior.coords),
        [ring(r.coords) for r in polygon.interiors],
    )


def _section_path(mesh, z: float):
    """Build the same trimesh path with one COO-to-CSR conversion per section.

    Preserve vertex merging, leaf-first DFS and edge closure order from
    trimesh's lines_to_path/edges_to_path pipeline (MIT; see third-party
    notices). Only graph preparation moves outside the traversal loop.
    """
    import trimesh

    sections, transforms, faces = trimesh.intersections.mesh_multiplane(
        mesh=mesh, plane_origin=[0.0, 0.0, 0.0], plane_normal=[0.0, 0.0, 1.0], heights=[z],
    )
    return _path_from_section(sections[0], transforms[0], faces[0])


def _path_from_section(lines, transform, face_index):
    """Common path construction for scalar and bounded-batch intersections."""
    import numpy as np
    import trimesh
    from scipy.sparse.csgraph import depth_first_order
    from trimesh.constants import tol_path
    from trimesh.path import Path2D
    from trimesh.path.entities import Line

    if not len(lines):
        return None
    vertices = np.asarray(lines, dtype=np.float64).reshape(-1, 2)
    unique, inverse = trimesh.grouping.unique_rows(vertices, digits=tol_path.merge_digits)
    edges = np.sort(inverse.reshape(-1, 2), axis=1)
    graph = trimesh.graph.edges_to_coo(edges).tocsr().astype(np.float64, copy=False)
    degree = np.bincount(edges.ravel())
    visited = np.zeros(len(degree) + 1, dtype=bool)
    traversals = []
    for start in np.concatenate((np.flatnonzero(degree == 1), np.flatnonzero(degree > 1))):
        if visited[start]:
            continue
        ordered = depth_first_order(
            graph, i_start=start, return_predecessors=False, directed=False,
        ).astype(np.int64)
        traversals.append(ordered)
        visited[ordered] = True
    connected = trimesh.graph.fill_traversals(traversals, edges)
    return Path2D(
        entities=[Line(nodes) for nodes in connected], vertices=vertices[unique], process=False,
        metadata={"to_3D": transform, "face_index": face_index},
    )


def section_track_metrics_by_height(mesh, heights, *, batch_size: int = 32) -> list[dict]:
    """All section tracks, including open/branched entities, at exact heights.

    Mark length counts every Line entity, not Path2D.discrete (which can omit
    open branches). Jumps retain the section entity order; this is an explicit
    STL-derived ordering, NOT a recovered machine trajectory or a timing bound.
    Entry/exit, between-instance/phase travel and hardware delays are absent.
    No support activation, recipe or physical layer index is inferred here.

    A bounded multiplane batch reuses intersection preparation without changing
    path construction, coordinates, order, duplicates or empty-height positions.
    Closed support sections are tracks too, not implicitly filled body regions.
    """
    import numpy as np
    import trimesh

    zs = np.asarray(heights, dtype=float)
    if zs.ndim != 1 or not np.isfinite(zs).all():
        raise EstimationError("Высоты сечений должны быть конечным одномерным массивом")
    if type(batch_size) is not int or not 1 <= batch_size <= 128:
        raise EstimationError("Размер пакета сечений должен быть целым числом от 1 до 128")
    result = []
    for offset in range(0, len(zs), batch_size):
        sections, transforms, faces = trimesh.intersections.mesh_multiplane(
            mesh=mesh, plane_origin=[0.0, 0.0, 0.0], plane_normal=[0.0, 0.0, 1.0],
            heights=zs[offset:offset + batch_size],
        )
        for lines, transform, face_index in zip(sections, transforms, faces, strict=True):
            path = _path_from_section(lines, transform, face_index)
            if path is None or not len(path.entities):
                result.append({'mark_mm': 0.0, 'jump_mm': 0.0, 'n_jumps': 0, 'n_tracks': 0})
                continue
            vertices = path.vertices @ transform[:2, :2].T + transform[:2, 3]
            if not np.isfinite(vertices).all():
                raise EstimationError("Неконечные координаты траекторий поддержки")
            entities = [entity.points for entity in path.entities]
            starts = np.concatenate([points[:-1] for points in entities])
            ends = np.concatenate([points[1:] for points in entities])
            mark = float(np.linalg.norm(vertices[ends] - vertices[starts], axis=1).sum())
            jumps = (vertices[[points[0] for points in entities[1:]]]
                     - vertices[[points[-1] for points in entities[:-1]]])
            result.append({'mark_mm': mark,
                'jump_mm': float(np.linalg.norm(jumps, axis=1).sum()),
                'n_jumps': int(np.count_nonzero(np.any(jumps != 0, axis=1))),
                'n_tracks': len(entities)})
    return result


def _section_polygons(mesh, z: float):
    """Closed plate-XY polygons and open track length, without changing topology.

    One multiplane intersection/path construction per body and level. Vertex
    merge tolerance, frame, contour repair and open/closed accounting remain
    those of the previous section_multiplane path.
    """
    import shapely.geometry

    path = _section_path(mesh, z)
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
    meshes: list, z: float, hatcher, z_bounds: list[tuple[float, float]] | None = None,
) -> tuple[float, float, float, float, float, list[float]]:
    """Co-hatch every body's closed sections at z; returns geometry components."""
    import numpy as np
    import pyslm
    import pyslm.analysis

    all_polygons: list = []
    open_len = 0.0
    body_boundary = []
    for index, mesh in enumerate(meshes):
        # Avoid slicing a body wholly outside this level. Bounds are captured
        # once for the plate; reading trimesh's bounds cache in every section
        # costs as much as some empty intersections. Keep near-boundary slices
        # unchanged, including thin sheets and coincident vertices.
        if z_bounds is not None:
            low, high = z_bounds[index]
            if z < low - _FIX_EPS or z > high + _FIX_EPS:
                body_boundary.append(0.0)
                continue
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
    *,
    contours_enabled: bool = True,
    hatch_angle_deg: float = DEFAULT_HATCH_ANGLE_DEG,
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
    geometry_options = scan_geometry_options({
        "contours_enabled": contours_enabled, "hatch_angle_deg": hatch_angle_deg,
    })

    z_bounds = [(float(m.bounds[0][2]), float(m.bounds[1][2])) for m in meshes]
    geometry_z_min = min(low for low, _ in z_bounds)
    z_max = max(high for _, high in z_bounds)
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
    for low, high in z_bounds:
        for zb in (low + pad, high - pad):
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
                pool.submit(_hatch_level, meshes, z,
                            _make_hatcher(hatch_distance_mm, **geometry_options), z_bounds): i
                for i, z in enumerate(zs)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    else:
        hatcher = _make_hatcher(hatch_distance_mm, **geometry_options)
        results = [_hatch_level(meshes, z, hatcher, z_bounds) for z in zs]

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
    if sum(series.hatch_mm) + sum(series.contour_mm) + sum(series.open_mm) <= 0:
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
    "scan_seconds_by_layer_from_model", "physics_scan_seconds_by_layer",
    "machine_cycle_from_layers", "DEFAULT_JUMP_SPEED_MM_S",
    "scan_geometry_options", "DEFAULT_HATCH_ANGLE_DEG",
    "section_track_metrics_by_height",
]
