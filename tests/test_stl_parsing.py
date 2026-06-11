"""Unit tests for STL volume helpers in api/routes/uploads.py (M2)."""
import struct

from api.routes.uploads import (
    _ascii_stl_volume,
    _binary_stl_volume,
    _is_binary_stl,
    _stl_volume_cm3,
)


def _make_binary_stl(triangles: list[tuple]) -> bytes:
    header = b"\x00" * 80
    count = struct.pack("<I", len(triangles))
    body = b""
    for v1, v2, v3 in triangles:
        normal = struct.pack("<3f", 0.0, 0.0, 0.0)
        verts = struct.pack("<3f", *v1) + struct.pack("<3f", *v2) + struct.pack("<3f", *v3)
        attr = struct.pack("<H", 0)
        body += normal + verts + attr
    return header + count + body


def _make_ascii_stl(triangles: list[tuple]) -> str:
    lines = ["solid test"]
    for v1, v2, v3 in triangles:
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        for v in (v1, v2, v3):
            lines.append(f"      vertex {v[0]} {v[1]} {v[2]}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid test")
    return "\n".join(lines)


# Simple tetrahedron: vertices at (10,0,0), (0,10,0), (0,0,10) and origin.
# Outward-wound faces — faces touching the origin contribute 0 to the sum;
# the opposing face contributes exactly 10*10*10/6 = 1000/6 mm³.
# Analytical volume = det/6 = (10³)/6 = 500/3 ≈ 166.667 mm³.
_TETRA_TRIS = [
    ((0, 0, 0), (0, 10, 0), (10, 0, 0)),
    ((0, 0, 0), (10, 0, 0), (0, 0, 10)),
    ((0, 0, 0), (0, 0, 10), (0, 10, 0)),
    ((10, 0, 0), (0, 10, 0), (0, 0, 10)),
]
_TETRA_VOL_MM3 = 500.0 / 3.0  # ≈ 166.667
_TETRA_VOL_CM3 = _TETRA_VOL_MM3 / 1000.0


class TestBinaryStlVolume:
    def test_tetrahedron_exact_volume(self):
        data = _make_binary_stl(_TETRA_TRIS)
        vol = _binary_stl_volume(data)
        assert abs(vol - _TETRA_VOL_MM3) < 1e-3

    def test_too_short_returns_zero(self):
        assert _binary_stl_volume(b"\x00" * 10) == 0.0

    def test_crafted_oversized_count_is_clamped(self):
        """Header claiming 0xFFFFFFFF triangles must not loop forever."""
        data = _make_binary_stl(_TETRA_TRIS)
        # Overwrite count field with a huge number — should be clamped to real triangles.
        bad = data[:80] + struct.pack("<I", 0xFFFF_FFFF) + data[84:]
        vol = _binary_stl_volume(bad)
        # Count gets clamped to 4 (only 4 triangles worth of bytes present).
        assert abs(vol - _TETRA_VOL_MM3) < 1e-3

    def test_header_only_body_empty_with_nonzero_count(self):
        """No triangle bytes after header → volume is 0, no crash."""
        header = b"\x00" * 80 + struct.pack("<I", 1_000_000)
        assert _binary_stl_volume(header) == 0.0


class TestAsciiStlVolume:
    def test_tetrahedron_exact_volume(self):
        text = _make_ascii_stl(_TETRA_TRIS)
        vol = _ascii_stl_volume(text)
        assert abs(vol - _TETRA_VOL_MM3) < 1e-3

    def test_empty_returns_zero(self):
        assert _ascii_stl_volume("solid empty\nendsolid") == 0.0

    def test_partial_triangle_not_counted(self):
        """Two vertices without a third must not crash or contribute volume."""
        text = "solid x\n  vertex 0 0 0\n  vertex 1 0 0\nendsolid"
        assert _ascii_stl_volume(text) == 0.0


class TestStlDispatch:
    def test_binary_dispatched_converts_to_cm3(self):
        data = _make_binary_stl(_TETRA_TRIS)
        assert _is_binary_stl(data)
        vol = _stl_volume_cm3(data)
        assert abs(vol - _TETRA_VOL_CM3) < 1e-6

    def test_ascii_dispatched_converts_to_cm3(self):
        text = _make_ascii_stl(_TETRA_TRIS)
        data = text.encode()
        assert not _is_binary_stl(data)
        vol = _stl_volume_cm3(data)
        assert abs(vol - _TETRA_VOL_CM3) < 1e-6
