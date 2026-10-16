import json
from statistics import median

import numpy as np
import pytest

from analytics.robust_stats import theil_sen_slope, theil_sen_slope_ci


@pytest.mark.parametrize("values,xs", [
    ([], None), ([5], None), ([None, float("nan"), float("inf")], None),
    ([1, 2, 3], [0, 0, 0]), ([1, 2], [0]), ([[1, 2], [3, 4]], None),
    (["bad", 2], None),
])
def test_unknown_slope_is_null_instead_of_flat_or_nonfinite(values, xs):
    assert theil_sen_slope(values, xs) is None
    assert theil_sen_slope_ci(values, xs) == (None, None, None)


@pytest.mark.parametrize("values,xs", [
    ([1, 3, None, 7], None), ([1, np.inf, 5, 7], None),
    ([1, 3, 5, 7], [0, np.nan, 2, 3]), ([1, 3, 3, 7], [0, 1, 1, 3]),
])
def test_slope_matches_independent_finite_pair_median(values, xs):
    x = list(range(len(values))) if xs is None else xs
    finite = [(a, b) for a, b in zip(x, values) if b is not None and np.isfinite(a) and np.isfinite(b)]
    slopes = [(b1 - b0) / (a1 - a0) for index, (a0, b0) in enumerate(finite)
              for a1, b1 in finite[index + 1:] if a1 != a0]
    expected = median(slopes)
    assert theil_sen_slope(values, xs) == pytest.approx(expected)
    assert theil_sen_slope_ci(values, xs)[0] == pytest.approx(expected)


def test_missing_rows_do_not_compress_implicit_x_coordinates():
    assert theil_sen_slope([0, None, 4, None, 8]) == 2


@pytest.mark.parametrize("values,expected", [([1, 5], 1), ([5, 5], 0)])
def test_two_observations_have_point_slope_without_fabricated_interval(values, expected):
    assert theil_sen_slope_ci(values, [0, 4]) == (expected, None, None)


def test_flat_trend_with_enough_observations_remains_measured_zero():
    assert theil_sen_slope_ci([5, 5, 5, 5]) == (0, 0, 0)


@pytest.mark.parametrize("max_points", [0, 1])
def test_invalid_regression_budget_does_not_create_flat_trend(max_points):
    assert theil_sen_slope([1, 2, 3], max_points=max_points) is None


def test_subsampling_keeps_physical_x_after_missing_rows():
    values = [3 * x + 2 if x % 3 else None for x in range(100)]
    assert theil_sen_slope(values, max_points=8) == pytest.approx(3)


def test_regression_failure_is_unknown_and_json_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("regression unavailable")
    monkeypatch.setattr("analytics.robust_stats.theilslopes", fail)
    result = theil_sen_slope_ci([1, 2, 3])
    assert result == (None, None, None)
    assert json.loads(json.dumps(result, allow_nan=False)) == [None, None, None]


def test_nonfinite_solver_result_is_not_published(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("analytics.robust_stats.theilslopes", lambda *a, **k:
                        SimpleNamespace(slope=float("nan"), low_slope=0, high_slope=1))
    assert theil_sen_slope_ci([1, 2, 3]) == (None, None, None)
