"""Track completeness and diagnostic arithmetic, not native slicer parity."""
import numpy as np
import pytest

trimesh = pytest.importorskip('trimesh')

from analytics.prediction.layer_engine import section_track_metrics_by_height  # noqa: E402
from analytics.prediction.stl_slicer import EstimationError  # noqa: E402
from scripts.research.probe_rs320_phases import error_summary  # noqa: E402


@pytest.mark.parametrize('kind', ['branches', 'annulus'])
def test_batch_keeps_all_entities_and_exact_height_order(kind):
    if kind == 'annulus':
        mesh = trimesh.creation.annulus(r_min=3, r_max=6, height=4, sections=16)
    else:
        vertices, faces = [], []
        for start, end in [((0, 0), (3, 0)), ((3, 0), (6, 0)), ((3, 0), (3, 4))]:
            i = len(vertices)
            vertices.extend([(*start, 0), (*end, 0), (*end, 2), (*start, 2)])
            faces.extend([(i, i + 1, i + 2), (i, i + 2, i + 3)])
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.apply_translation([73, -29, .4])
    original = mesh.vertices.copy()
    heights = [.9, 10., 1.2, .9]
    expected = mesh.section_multiplane(plane_origin=[0, 0, 0], plane_normal=[0, 0, 1], heights=heights)
    actual = section_track_metrics_by_height(mesh, heights, batch_size=2)
    assert np.array_equal(mesh.vertices, original)
    assert actual[0] == actual[3]
    assert actual[1] == {'mark_mm': 0., 'jump_mm': 0., 'n_jumps': 0, 'n_tracks': 0}
    for section, metrics in zip(expected, actual, strict=True):
        if section is None:
            continue
        curves = [entity.discrete(section.vertices) for entity in section.entities]
        curves_xy = [trimesh.transform_points(np.column_stack((curve, np.zeros(len(curve)))),
                                              section.metadata['to_3D'])[:, :2] for curve in curves]
        mark = sum(float(np.linalg.norm(np.diff(c, axis=0), axis=1).sum()) for c in curves_xy)
        jumps = [right[0] - left[-1] for left, right in zip(curves_xy, curves_xy[1:])]
        assert metrics['mark_mm'] == pytest.approx(mark, rel=1e-12, abs=1e-10)
        assert metrics['jump_mm'] == pytest.approx(sum(float(np.linalg.norm(j)) for j in jumps))
        assert metrics['n_tracks'] == len(curves)
        assert metrics['n_jumps'] == sum(bool(np.any(j != 0)) for j in jumps)
    if kind == 'branches':
        assert actual[0]['mark_mm'] == pytest.approx(10.)
        assert sum(float(np.linalg.norm(np.diff(c, axis=0), axis=1).sum())
                   for c in expected[0].discrete) < actual[0]['mark_mm']


def test_empty_height_request_and_closed_track_are_not_filled_or_rehatched():
    mesh = trimesh.creation.box(extents=[20, 10, 2])
    assert section_track_metrics_by_height(mesh, []) == []
    result, = section_track_metrics_by_height(mesh, [.25])
    assert result == {'mark_mm': 60., 'jump_mm': 0., 'n_jumps': 0, 'n_tracks': 1}


@pytest.mark.parametrize('heights,batch', [([float('nan')], 2), ([[1., 2.]], 2),
    ([1.], 0), ([1.], 129), ([1.], True)])
def test_bad_heights_or_batch_fail_before_intersection(heights, batch):
    with pytest.raises(EstimationError):
        section_track_metrics_by_height(None, heights, batch_size=batch)


def test_opposite_errors_cannot_hide_behind_matching_sum():
    result = error_summary(np.array([80., 120.]), np.array([100., 100.]))
    assert result['burn_sum_error_pct'] == 0
    assert result['burn_wape_pct'] == 20
    assert result['median_absolute_error_seconds'] == 20
