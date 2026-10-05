"""Fixtures for the recovered instruction semantics, not slicer accuracy."""
import pytest

from scripts.research.probe_laserstudio_rules import replay_cone_active, replay_hatch_angle


@pytest.mark.parametrize('index,angle,tabu,expected', [
    (0, 67, 0, 67), (1, 67, 0, 10), (2, 67, 0, 43), (3, 67, 0, -14),
    (1, -133, 0, -100),  # C truncation to zero, not Python floor division.
    (0, 5, 0, 5), (0, -5, 0, -5),  # Strict tabu boundaries.
    (0, 0, 0, 10), (0, 0, 10, -10), (0, 0, -10, 10),
    (0, 5, 10, 5), (0, 15, 10, 15), (0, 10, 10, 20),
])
def test_variable_angle(index, angle, tabu, expected):
    assert replay_hatch_angle(slice_index=index, mode=0, angle=angle, tabu_angle=tabu) == expected


def test_fixed_and_alternating_return_before_tabu_rules():
    assert replay_hatch_angle(slice_index=3, mode=2, angle=0, tabu_angle=0) == 0
    assert [replay_hatch_angle(slice_index=k, mode=1, angle=0, tabu_angle=45)
            for k in range(4)] == [45, -45, 45, -45]


@pytest.mark.parametrize('skip,expected', [
    (0, [True] * 6), (1, [True, False] * 3),
    (2, [True, False, False] * 2),
])
def test_periodicity_is_explicitly_cones_only(skip, expected):
    assert [replay_cone_active(slice_index=k, skip=skip) for k in range(6)] == expected


def test_unknown_state_and_unsafe_integer_inputs_are_not_guessed():
    for kwargs in ({'mode': 3}, {'slice_index': -1}, {'angle': True},
                   {'angle': 1.5}, {'angle': 2**31}, {'slice_index': 2**31 - 1},
                   {'tabu_angle': 2**31 - 1}):
        with pytest.raises(ValueError):
            replay_hatch_angle(**({'slice_index': 1, 'mode': 0, 'angle': 67,
                                  'tabu_angle': 0} | kwargs))
    for skip in (-1, True, 2**31 - 1):
        with pytest.raises(ValueError):
            replay_cone_active(slice_index=0, skip=skip)
