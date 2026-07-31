#!/usr/bin/env python
"""Read part geometry out of a Materialise Magics project (``.magics``).

There is no public parser for this format — Materialise documents it as closed,
and the only third-party option is a paid conversion service. It turns out the
container is barely obfuscated:

**A .magics file is a ZIP archive with the ``PK`` signature replaced by ``MT``.**
Swap the four-byte structure signatures back (``MT\\x03\\x04`` → ``PK\\x03\\x04``
and friends) and every entry decompresses with correct CRCs.

Entry layout, verified against the STL exports of the same plate:

``Stl{<guid>}_vertices``
    int32 little-endian triples, unit 1e-4 mm. Not float32 — that was the first
    guess and it decodes to zeros.
``Stl{<guid>}_surfaces``
    uint32 little-endian triples: triangle vertex indices.
``Stl{<guid>}_graphs``
    small index table, not needed for geometry.
``SupportSurfaces_ox<address>``
    support geometry, 22 entries / 5.6 MB on the test plate. Index-like data
    behind a header of unknown length; **not decoded** (sizes are not a
    multiple of 12, so it is not the same layout as the parts).
``SemiautomaticTreeSurfaceData_ConnectionPoints_<address>``
    looks like float64 support anchor points — parametric rather than meshed.
``header.xml``
    despite the name, **encrypted**: 7.94 bits/byte of entropy, and 68% of its
    16-byte blocks repeat, which is the signature of an ECB-mode block cipher.
    This is where part names, placement transforms and machine parameters live.
    Nothing here tries to break it.

What that means in practice: the meshes come out exact but *unplaced* and
*unnamed*. Verified on three parts of the 27.05 plate — volume and surface area
match the STL exports to 0.0000%, while the bounding boxes differ, because the
STL export bakes in a placement this reader cannot recover.

Usage::

    python scripts/read_magics.py plate.magics                 # list entries
    python scripts/read_magics.py plate.magics --export out/   # parts as STL
"""
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path

# Every four-byte ZIP structure signature, with PK swapped for MT. Replacing
# only these keeps deflate payloads untouched; a blanket "MT" → "PK" would
# corrupt them. Any damage would surface anyway — CRCs are checked on read.
_SIGNATURES = (
    b"\x03\x04",  # local file header
    b"\x01\x02",  # central directory
    b"\x05\x06",  # end of central directory
    b"\x07\x08",  # data descriptor
    b"\x06\x06",  # zip64 end of central directory
    b"\x06\x07",  # zip64 end of central directory locator
)

_PART_RE = re.compile(r"^Stl\{(?P<guid>[0-9a-fA-F-]+)\}_(?P<kind>vertices|surfaces|graphs)$")

# Magics stores coordinates as integers in tenths of a micrometre.
VERTEX_SCALE_MM = 1e-4


def open_container(path: str | Path) -> zipfile.ZipFile:
    """Open a .magics file as the ZIP archive it actually is.

    Reads the whole file into memory — projects are tens of MB, and the
    signature patch has to happen before ZIP's central directory is parsed.
    """
    raw = Path(path).read_bytes()
    if raw[:4] == b"PK\x03\x04":
        return zipfile.ZipFile(io.BytesIO(raw))  # already a plain zip
    if raw[:2] != b"MT":
        raise ValueError(f"{path}: not a Magics container (starts with {raw[:4]!r})")
    for signature in _SIGNATURES:
        raw = raw.replace(b"MT" + signature, b"PK" + signature)
    return zipfile.ZipFile(io.BytesIO(raw))


def part_meshes(zf: zipfile.ZipFile) -> dict[str, dict]:
    """``{guid: {"vertices": (N,3) float mm, "faces": (M,3) int}}`` for every part.

    Vertices are in the part's own frame: the placement on the build platform
    is in the encrypted header and cannot be recovered here.
    """
    import numpy as np

    parts: dict[str, dict] = {}
    for name in zf.namelist():
        match = _PART_RE.match(name)
        if not match or match["kind"] == "graphs":
            continue
        guid = match["guid"]
        entry = parts.setdefault(guid, {})
        if match["kind"] == "vertices":
            raw = np.frombuffer(zf.read(name), dtype="<i4").reshape(-1, 3)
            entry["vertices"] = raw.astype(np.float64) * VERTEX_SCALE_MM
        else:
            entry["faces"] = np.frombuffer(zf.read(name), dtype="<u4").reshape(-1, 3)
    return {g: p for g, p in parts.items() if "vertices" in p and "faces" in p}


def _list(zf: zipfile.ZipFile) -> None:
    groups: dict[str, list[zipfile.ZipInfo]] = {}
    for info in zf.infolist():
        if _PART_RE.match(info.filename):
            key = "детали"
        elif info.filename.startswith("SupportSurfaces_"):
            key = "поддержки (не декодированы)"
        elif "ConnectionPoints" in info.filename:
            key = "точки крепления поддержек"
        elif info.filename == "header.xml":
            key = "header.xml (зашифрован)"
        else:
            key = "прочее"
        groups.setdefault(key, []).append(info)
    for key, infos in groups.items():
        total = sum(i.file_size for i in infos)
        print(f"{key:32s} {len(infos):4d} записей  {total / 1e6:8.2f} МБ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="файл .magics")
    parser.add_argument("--export", metavar="DIR", help="выгрузить детали в STL")
    args = parser.parse_args()

    zf = open_container(args.path)
    damaged = zf.testzip()
    if damaged is not None:
        raise SystemExit(f"повреждённая запись: {damaged}")

    _list(zf)
    parts = part_meshes(zf)
    print(f"\nдеталей с геометрией: {len(parts)}")
    for guid, part in sorted(parts.items(), key=lambda kv: -len(kv[1]["faces"])):
        v, f = part["vertices"], part["faces"]
        size = v.max(axis=0) - v.min(axis=0)
        print(f"  {guid}  V={len(v):7d} F={len(f):7d}  "
              f"габарит {size[0]:7.2f} x {size[1]:7.2f} x {size[2]:7.2f} мм")

    if args.export:
        import trimesh

        out = Path(args.export)
        out.mkdir(parents=True, exist_ok=True)
        for guid, part in parts.items():
            mesh = trimesh.Trimesh(vertices=part["vertices"], faces=part["faces"],
                                   process=False)
            mesh.export(out / f"{guid}.stl")
        print(f"\nвыгружено {len(parts)} STL в {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
