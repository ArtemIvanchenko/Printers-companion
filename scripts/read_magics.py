#!/usr/bin/env python
"""Read a Materialise Magics project (``.magics``) without Magics.

There is no public parser for this format. Materialise documents it as closed;
the third-party "converters" that come up in search do not read it either —
CAD Exchanger's *Magics to STL* page turns out to describe Magics' own STL
export, and ``.magics`` appears nowhere in its 30+ format list.

Everything below was derived from a corpus of 20 real project files and, where
marked *verified*, checked against the STL exports of the same plates.

The container
-------------
**A .magics file is a ZIP archive with the ``PK`` signature replaced by ``MT``.**
Swap the six four-byte structure signatures back and every entry decompresses
with a correct CRC — all 20 files.

Entries
-------
``Stl{<guid>}_vertices``   int32 triples, unit 1e-4 mm. *Not* float32 — that was
                           the first guess and it decodes to zeros.
``Stl{<guid>}_surfaces``   uint32 triples: triangle vertex indices.
``Stl{<guid>}_graphs``     uint32 quads ``(i+k, i, i, i-k)`` for a fixed k —
                           neighbour links across a support lattice, not needed
                           for geometry.
``SupportSurfaces_ox<addr>``
                           per-support record, five sections; see `parse_support`.
                           Named by the object's address in the Magics process.
``SemiautomaticTreeSurfaceData_ConnectionPoints_<addr>``
                           uint32 count + count records of 48 bytes = six
                           float64: point (x, y, z) in 1e-4 mm and a unit normal.
``preview_128x128`` / ``preview_256x256``
                           ordinary JPEG.
``blob_<n>``               polymorphic slot — empty, an 8-byte count+value pair,
                           16 uint32 of support parameters, or an RGB bitmap.
``header.xml``             despite the name, **encrypted**; see `HEADER_NOTES`.

Verified against ground truth
-----------------------------
* Part meshes: volume and surface area match the STL exports to **0.0000 %**.
* Support facet indices: every index lands inside the part it belongs to
  (max 3369 of 4040 facets on one part, 31141 of 34542 on the other), and two
  instances of one support on two copies of a part differ by a constant index
  offset.
* Support footprints: the int32 XY section reproduces ``s_*.stl`` bounding boxes
  exactly — x[30.86, 76.62] y[18.79, 62.01] mm on the 27.05 plate.
* Support heights: the maximum Z of the float section matches the support STL's
  maximum Z to **0.0000 mm** on the entries whose footprint does not overlap a
  neighbouring support.
* Connection points: z = 37.6 mm against a support top of 37.72 mm in the STL,
  unit normals to 1.000000, and the record layout fits every entry of the corpus.

What is *not* recovered: part names, placement transforms and machine
parameters, which all live in the encrypted header. Meshes therefore come out
exact but unnamed, and in the project's own plate frame rather than the
placement an STL export bakes in.

Usage::

    python scripts/read_magics.py plate.magics                 # inventory
    python scripts/read_magics.py plate.magics --supports      # support detail
    python scripts/read_magics.py plate.magics --export out/   # parts as STL
"""
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path

# Every four-byte ZIP structure signature, with PK swapped for MT. Replacing
# only these keeps deflate payloads untouched; a blanket "MT" -> "PK" would
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

# Magics stores coordinates as integers in tenths of a micrometre, both in the
# part meshes and in the support sections.
UNIT_MM = 1e-4

HEADER_NOTES = """\
header.xml is encrypted, but not the way a first look suggests.

Measured over the 20-file corpus:
  * entropy 7.92-7.96 bits/byte, so it looks random byte for byte;
  * yet only 5.1% of its 4-byte words are distinct (14164 of 275623 on the
    27.05 plate) and the most common word repeats 3399 times;
  * consequently it still compresses — zlib to 21.6%, lzma to 6.7%. Real
    ciphertext does not compress at all;
  * lengths are not a multiple of 4, 8 or 16, so it is not a plain block
    cipher over the whole payload;
  * across files the vocabularies are disjoint: not one 4-byte word is shared
    by any two of the 20 files.

Together that means a deterministic, position-independent transform over
4-byte units under a per-file key — ECB-like leakage at 32-bit granularity,
with the trailing bytes left over. It leaks plaintext repetition wholesale,
which is exactly why it still compresses.

Recovering the plaintext from here would be a codebook or known-plaintext
attack on a commercial product's protection, and needs the key, which lives in
the Magics binary. Nothing in this file attempts either.
"""


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

    Vertices are in the project's plate frame. The placement an STL export bakes
    in lives in the encrypted header and is not recoverable here.
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
            entry["vertices"] = raw.astype(np.float64) * UNIT_MM
        else:
            entry["faces"] = np.frombuffer(zf.read(name), dtype="<u4").reshape(-1, 3)
    return {g: p for g, p in parts.items() if "vertices" in p and "faces" in p}


def connection_points(zf: zipfile.ZipFile) -> dict[str, dict]:
    """Tree-support anchor points: ``{entry: {"xyz": (N,3) mm, "normal": (N,3)}}``.

    ``uint32 count`` followed by count records of six float64 — point, then unit
    normal. Fits every entry in the corpus exactly.
    """
    import numpy as np

    out: dict[str, dict] = {}
    for name in zf.namelist():
        if "ConnectionPoints" not in name:
            continue
        raw = zf.read(name)
        count = int(np.frombuffer(raw[:4], dtype="<u4")[0])
        if len(raw) - 4 != count * 48:
            continue  # unknown variant; skip rather than mis-report it
        rec = np.frombuffer(raw[4:], dtype="<f8").reshape(count, 6)
        out[name] = {"xyz": rec[:, :3] * UNIT_MM, "normal": rec[:, 3:]}
    return out


# --- supports ---------------------------------------------------------------
#
# A SupportSurfaces entry is a byte stream — fields are written back to back
# with no padding, which is why the float section usually starts off a 4-byte
# boundary. Five sections, in order:
#
#   1. uint32   facet indices into the supported part's mesh
#   2. int32    XY pairs, 1e-4 mm, absolute plate coordinates: 16 bytes (two
#               points) per segment of the support's footprint
#   3. uint8    one type code per segment, observed values 1..4
#   4. float32  stride-6 records (t0, t1, z_bottom0, z_bottom1, z_top0, z_top1):
#               the support wall profile along the footprint. t runs 0->1 along
#               the contour and chains between consecutive records, as does the
#               z_top pair. Where z_bottom equals z_top the segment carries no
#               support. z_bottom is frequently exactly 3.00 mm, which is *not*
#               where the support actually ends — the exported meshes start at
#               z = 0.00 — so treat it as a parameter, not as geometry.
#   5.          a bitmask — long runs of 0x00/0xff with partial bytes at the
#               boundaries. Its subject is not established.
#
# Section lengths are not stored inline, so the boundaries are recovered from
# content. That works because the three payload types are disjoint in range:
# indices are small, coordinates sit inside the plate, and the float section
# holds only 0, a parameter in (0, 1], or a Z in 1e-4 mm.
#
# It stays a heuristic. Across the corpus the coordinate boundary lands exactly
# on a segment in 666 of 684 supports; the other 18 overrun by 8 or 12 bytes,
# which `coord_overrun` reports. The right-hand edge of the float section is the
# weaker guess of the two: it is right wherever a support's footprint does not
# overlap a neighbour's, and those are the entries the STL check confirms.

_SMALL_INDEX = 200_000       # facet indices stay well below this
_PLATE_MAX = 5_000_000       # 500 mm in 1e-4 mm
_Z_MAX = 6_000_000           # 600 mm, a generous ceiling for a support height


def parse_support(data: bytes) -> dict:
    """Segment one ``SupportSurfaces_*`` entry. See the note above for the layout."""
    import numpy as np

    words = np.frombuffer(data[: len(data) // 4 * 4], dtype="<u4")

    i = 0
    while i < len(words) and words[i] < _SMALL_INDEX:
        i += 1
    idx_end = i * 4

    j = i
    while j < len(words) and 0 < words[j] < _PLATE_MAX:
        j += 1
    coord_end = j * 4

    n_seg = (coord_end - idx_end) // 16
    f_start = coord_end + n_seg               # one type byte per segment
    f_end = _float_end(data, f_start) if f_start < len(data) else f_start

    # Two points per segment, so take exactly n_seg * 4 int32. The range scan
    # can overrun by a word or two into the type bytes; those are not
    # coordinates, and slicing by the segment count drops them.
    coords = np.frombuffer(data[idx_end : idx_end + n_seg * 16], dtype="<i4")
    xy = coords.astype(np.float64).reshape(-1, 2) * UNIT_MM

    floats = np.frombuffer(data[f_start : f_start + (f_end - f_start) // 4 * 4], dtype="<f4")
    heights = floats[floats > 1000].astype(np.float64) * UNIT_MM

    return {
        "facets": np.frombuffer(data[:idx_end], dtype="<u4"),
        "n_segments": n_seg,
        "footprint_xy": xy,
        "coord_overrun": coord_end - (idx_end + n_seg * 16),
        "types": np.frombuffer(data[coord_end:f_start], dtype=np.uint8),
        "z": heights,
        "mask_bytes": len(data) - f_end,
        "size": len(data),
    }


def _float_end(data: bytes, start: int) -> int:
    """First offset past the float section.

    The mask that follows is dense in 0x00/0xff, which decodes to NaN, inf or
    absurd magnitudes — so the first float outside the three legal ranges ends
    the section.
    """
    import numpy as np

    n = (len(data) - start) // 4
    if n <= 0:
        return start
    f = np.frombuffer(data[start : start + n * 4], dtype="<f4")
    with np.errstate(invalid="ignore"):
        legal = (f == 0) | ((f > 0) & (f <= 1.0001)) | ((f >= 1000) & (f <= _Z_MAX))
    bad = np.flatnonzero(~legal)
    return start + (int(bad[0]) * 4 if len(bad) else n * 4)


def _inventory(zf: zipfile.ZipFile) -> None:
    groups: dict[str, list[zipfile.ZipInfo]] = {}
    for info in zf.infolist():
        name = info.filename
        if _PART_RE.match(name):
            key = "детали"
        elif name.startswith("SupportSurfaces_"):
            key = "поддержки"
        elif "ConnectionPoints" in name:
            key = "точки крепления поддержек"
        elif name.startswith("preview"):
            key = "превью (JPEG)"
        elif name.startswith("blob_"):
            key = "blob"
        elif name == "header.xml":
            key = "header.xml (зашифрован)"
        else:
            key = "прочее"
        groups.setdefault(key, []).append(info)
    for key, infos in groups.items():
        total = sum(i.file_size for i in infos)
        print(f"{key:28s} {len(infos):4d} записей  {total / 1e6:8.2f} МБ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="файл .magics")
    parser.add_argument("--export", metavar="DIR", help="выгрузить детали в STL")
    parser.add_argument("--supports", action="store_true", help="разобрать поддержки")
    parser.add_argument("--header-notes", action="store_true",
                        help="что известно про шифрование header.xml")
    args = parser.parse_args()

    if args.header_notes:
        print(HEADER_NOTES)

    zf = open_container(args.path)
    damaged = zf.testzip()
    if damaged is not None:
        raise SystemExit(f"повреждённая запись: {damaged}")

    _inventory(zf)

    parts = part_meshes(zf)
    print(f"\nдеталей с геометрией: {len(parts)}")
    for guid, part in sorted(parts.items(), key=lambda kv: -len(kv[1]["faces"])):
        v, f = part["vertices"], part["faces"]
        size = v.max(axis=0) - v.min(axis=0)
        print(f"  {guid}  V={len(v):7d} F={len(f):7d}  "
              f"габарит {size[0]:7.2f} x {size[1]:7.2f} x {size[2]:7.2f} мм")

    points = connection_points(zf)
    if points:
        total = sum(len(p["xyz"]) for p in points.values())
        print(f"\nточек крепления поддержек: {total} в {len(points)} записях")

    if args.supports:
        names = sorted(n for n in zf.namelist() if n.startswith("SupportSurfaces_"))
        print(f"\nподдержек: {len(names)}")
        for name in names:
            s = parse_support(zf.read(name))
            xy, z = s["footprint_xy"], s["z"]
            if not len(xy) or not len(z):
                print(f"  {name[16:]:22s} граней={len(s['facets']):7d} "
                      f"сегм.={s['n_segments']:5d}  (только индексы)")
                continue
            lo, hi = xy.min(axis=0), xy.max(axis=0)
            print(f"  {name[16:]:22s} граней={len(s['facets']):7d} "
                  f"сегм.={s['n_segments']:5d}  "
                  f"XY [{lo[0]:7.2f},{hi[0]:7.2f}] x [{lo[1]:6.2f},{hi[1]:6.2f}]  "
                  f"Z [{z.min():6.2f},{z.max():6.2f}] мм")

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
