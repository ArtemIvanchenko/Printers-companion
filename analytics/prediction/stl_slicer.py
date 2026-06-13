"""STL slicing for print-time estimation, built on trimesh + shapely.

Produces per-layer cross-section areas and perimeters. Sections are sampled
(at most _MAX_SECTIONS evenly spaced heights) so very tall parts stay fast;
estimators integrate over the samples and scale to the true layer count.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Cap on the number of cross-sections actually computed; the M-450M at
# 0.03–0.06 mm layers would otherwise need 5 000–10 000 sections per request.
_MAX_SECTIONS = 400

# MeshFix repair runs in C++ and scales poorly: a 1.3M-face support mesh takes
# minutes and usually yields nothing useful anyway. Skip repair above this size
# so the (often background) request never stalls — a clean part STL is far smaller.
_MAX_REPAIR_FACES = 300_000


@dataclass
class SliceResult:
    volume_mm3: float
    height_mm: float
    layer_count: int            # true count: ceil(height / layer_thickness)
    layer_thickness_mm: float
    # Sampled sections (len ≤ _MAX_SECTIONS), evenly spaced bottom→top:
    section_zs: list[float] = field(default_factory=list)
    section_areas_mm2: list[float] = field(default_factory=list)
    section_perimeters_mm: list[float] = field(default_factory=list)
    is_watertight: bool = True
    was_repaired: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def avg_area_mm2(self) -> float:
        return sum(self.section_areas_mm2) / len(self.section_areas_mm2) if self.section_areas_mm2 else 0.0

    @property
    def avg_perimeter_mm(self) -> float:
        return sum(self.section_perimeters_mm) / len(self.section_perimeters_mm) if self.section_perimeters_mm else 0.0


def load_mesh(stl_bytes: bytes):
    """Load STL bytes into a trimesh mesh (raises ValueError when unparseable)."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=True)
    if mesh.is_empty or len(mesh.faces) == 0:
        raise ValueError("STL не содержит геометрии")
    return mesh


def _repair_mesh(mesh):
    """Make a non-watertight mesh watertight with MeshFix.

    Returns ``(repaired_mesh, True)`` on success or ``(mesh, False)`` when
    repair is unavailable or did not help. Production STL exported from Magics
    (especially support bodies) often have holes/self-intersections that throw
    off volume and cross-section area.
    """
    if len(mesh.faces) > _MAX_REPAIR_FACES:
        logger.info("mesh repair skipped: %d faces exceeds %d", len(mesh.faces), _MAX_REPAIR_FACES)
        return mesh, False
    try:
        import trimesh
        from pymeshfix import MeshFix
    except Exception:
        return mesh, False

    try:
        fixer = MeshFix(mesh.vertices, mesh.faces)
        fixer.repair()
        repaired = trimesh.Trimesh(vertices=fixer.points, faces=fixer.faces, process=True)
        if repaired.is_empty or len(repaired.faces) == 0:
            return mesh, False
        return repaired, bool(repaired.is_watertight)
    except Exception:
        logger.exception("pymeshfix repair failed")
        return mesh, False


def slice_stl(stl_bytes: bytes, layer_thickness_mm: float) -> SliceResult:
    """Slice an STL (millimetre units) into cross-sections.

    Uses trimesh ``section_multiplane`` → shapely polygons; area includes
    holes correctly, perimeter counts exterior + interior boundaries.
    """
    if layer_thickness_mm <= 0:
        raise ValueError("Толщина слоя должна быть > 0")
    mesh = load_mesh(stl_bytes)

    warnings: list[str] = []
    repaired = False
    if not mesh.is_watertight:
        too_big = len(mesh.faces) > _MAX_REPAIR_FACES
        mesh, repaired = _repair_mesh(mesh)
        if repaired:
            warnings.append("Сетка не была герметична — автоматически починена (MeshFix).")
        elif too_big:
            warnings.append(
                f"Сетка не герметична и слишком большая для автопочинки "
                f"(>{_MAX_REPAIR_FACES // 1000} тыс. граней — вероятно, файл с поддержками). "
                "Загрузите оригинальный STL детали без поддержек."
            )
        else:
            warnings.append(
                "Сетка не герметична (есть дыры) и не поддалась автопочинке — "
                "объём и сечения могут быть неточными."
            )

    z_min, z_max = float(mesh.bounds[0][2]), float(mesh.bounds[1][2])
    height = max(z_max - z_min, 0.0)
    layer_count = max(int(height / layer_thickness_mm + 0.999999), 1)

    n_sections = min(layer_count, _MAX_SECTIONS)
    # Section through layer mid-heights to dodge tangent faces at z_min/z_max
    step = height / n_sections if n_sections else layer_thickness_mm
    zs = [z_min + (i + 0.5) * step for i in range(n_sections)]

    areas: list[float] = []
    perimeters: list[float] = []
    sections = mesh.section_multiplane(
        plane_origin=[0, 0, 0], plane_normal=[0, 0, 1], heights=zs,
    )
    for path2d in sections:
        if path2d is None:
            areas.append(0.0)
            perimeters.append(0.0)
            continue
        # polygons_full: shapely polygons with holes already resolved
        polys = path2d.polygons_full
        areas.append(float(sum(p.area for p in polys)))
        perimeters.append(float(sum(p.exterior.length + sum(r.length for r in p.interiors) for p in polys)))

    if areas and sum(areas) == 0.0:
        warnings.append("Все сечения пустые — проверьте ориентацию и масштаб модели.")

    return SliceResult(
        volume_mm3=float(abs(mesh.volume)),
        height_mm=height,
        layer_count=layer_count,
        layer_thickness_mm=layer_thickness_mm,
        section_zs=zs,
        section_areas_mm2=areas,
        section_perimeters_mm=perimeters,
        is_watertight=bool(mesh.is_watertight),
        was_repaired=repaired,
        warnings=warnings,
    )


__all__ = ["SliceResult", "slice_stl", "load_mesh"]
