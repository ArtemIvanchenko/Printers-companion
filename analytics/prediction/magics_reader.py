"""Reader for Materialise Magics project files (.magics) — plate geometry.

Format, established empirically on this shop's files and cross-checked against
the operator's print archive (decoded plate height matched the recorded build
height exactly; part volume matched within 0.6%):

* Container: a ZIP archive whose signatures are branded ``MT`` where PKZIP
  writes ``PK`` (local file header, central directory, end record).
* ``Stl{uuid}_vertices``: little-endian int32 XYZ triples, unit = 0.1 µm
  (divide by 10 000 for millimetres). Plate coordinates.
* ``Stl{uuid}_surfaces``: little-endian uint32 triangle vertex indices.
* ``SupportSurfaces_*``: Magics' internal support definitions. These are NOT
  plain meshes (index/parameter streams, length not even word-aligned) and are
  deliberately not decoded — their presence is only *detected*, so callers can
  warn that the plate has supports whose geometry must come from the exported
  ``s_*.stl`` files instead.

  ``scripts/read_magics.py`` goes further and decodes this section's facet
  indices, XY footprint and Z wall profile (verified against the same
  ``s_*.stl`` exports). It is not wired in here: the per-segment type byte
  (values 1-4 observed) isn't decoded, and real SLM supports usually carry an
  internal perforation/teeth pattern for easy removal that a footprint+height
  reconstruction would miss entirely — likely *underestimating* scan time
  rather than matching it. Do not lean on it for this module without first
  validating the reconstructed hatch time against real burn_ms.

Bodies that extend below the platform (z < 0) are reference/marker geometry,
not printed parts; ``read_plate`` separates them out.
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_SIGS = (
    (b"MT\x03\x04", b"PK\x03\x04"),
    (b"MT\x01\x02", b"PK\x01\x02"),
    (b"MT\x05\x06", b"PK\x05\x06"),
)
_UNIT_MM = 1e-4  # int32 unit -> mm
# Bodies dipping below the platform by more than this are markers, not parts.
_BELOW_PLATFORM_MM = 0.5

_STL_ENTRY = re.compile(r"(Stl\{[0-9a-f-]{36}\})_vertices")


@dataclass
class MagicsPlate:
    """Decoded contents of one .magics project."""

    parts: list["trimesh.Trimesh"] = field(default_factory=list)  # noqa: F821
    markers: list["trimesh.Trimesh"] = field(default_factory=list)  # noqa: F821
    support_entry_count: int = 0  # undecodable native support definitions
    warnings: list[str] = field(default_factory=list)

    @property
    def has_native_supports(self) -> bool:
        return self.support_entry_count > 0


def is_magics_file(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"MT\x03\x04"
    except OSError:
        return False


def open_magics_zip(path: str | Path) -> zipfile.ZipFile:
    """Open a .magics file as the ZIP archive it really is."""
    raw = Path(path).read_bytes()
    for branded, standard in _SIGS:
        raw = raw.replace(branded, standard)
    return zipfile.ZipFile(io.BytesIO(raw))


def read_plate(path: str | Path) -> MagicsPlate:
    """Decode every printable body of the plate into trimesh meshes (mm)."""
    import numpy as np
    import trimesh

    z = open_magics_zip(path)
    names = set(z.namelist())
    plate = MagicsPlate(
        support_entry_count=sum(1 for n in names if n.startswith("SupportSurfaces")),
    )

    for name in sorted(names):
        m = _STL_ENTRY.fullmatch(name)
        if not m or f"{m.group(1)}_surfaces" not in names:
            continue
        vraw, sraw = z.read(name), z.read(f"{m.group(1)}_surfaces")
        if len(vraw) % 12 or len(sraw) % 12:
            plate.warnings.append(f"Запись {m.group(1)} повреждена — пропущена")
            continue
        vertices = np.frombuffer(vraw, dtype="<i4").reshape(-1, 3).astype(np.float64) * _UNIT_MM
        faces = np.frombuffer(sraw, dtype="<u4").reshape(-1, 3)
        if vertices.size == 0 or faces.size == 0 or faces.max() >= len(vertices):
            plate.warnings.append(f"Запись {m.group(1)} не согласована — пропущена")
            continue
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        if mesh.bounds[0][2] < -_BELOW_PLATFORM_MM:
            plate.markers.append(mesh)
        else:
            plate.parts.append(mesh)

    if plate.has_native_supports:
        plate.warnings.append(
            f"В компоновке заданы поддержки Magics ({plate.support_entry_count} зап.), но их "
            "геометрия хранится во внутреннем формате и не читается. Добавьте экспортированные "
            "s_*.stl файлы поддержек — без них расчёт даст НИЖНЮЮ границу времени."
        )
    if not plate.parts:
        plate.warnings.append("В файле не найдено ни одного печатаемого тела.")
    return plate


__all__ = ["MagicsPlate", "read_plate", "open_magics_zip", "is_magics_file"]
