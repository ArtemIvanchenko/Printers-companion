"""resolve_recoat_ms precedence: calibrated -> manual -> default.

Deliberately in its own file, not tests/analytics/test_print_prediction.py:
this function has no geometry dependency at all, and importing trimesh/pyslm
there would force an unrelated dependency onto a pure precedence test.
"""
from analytics.prediction.print_time import resolve_recoat_ms


class TestResolveRecoatMs:
    def test_nothing_set_falls_back_to_hardcoded_default(self):
        value, source = resolve_recoat_ms({}, "steel")
        assert value == 9500.0
        assert source == "default"

    def test_manual_value_wins_over_default(self):
        value, source = resolve_recoat_ms({"recoat_time_ms": 8000.0}, "steel")
        assert value == 8000.0
        assert source == "manual"

    def test_calibrated_per_material_value_wins_over_manual(self):
        params = {"recoat_time_ms": 8000.0, "recoat_time_by_mat": {"steel": 15000.0}}
        value, source = resolve_recoat_ms(params, "steel")
        assert value == 15000.0
        assert source == "calibrated"

    def test_calibrated_value_only_applies_to_its_own_material(self):
        params = {"recoat_time_ms": 8000.0, "recoat_time_by_mat": {"steel": 15000.0}}
        value, source = resolve_recoat_ms(params, "aluminum")
        assert value == 8000.0
        assert source == "manual"

    def test_zero_or_negative_calibrated_value_is_ignored(self):
        params = {"recoat_time_ms": 8000.0, "recoat_time_by_mat": {"steel": 0.0}}
        value, source = resolve_recoat_ms(params, "steel")
        assert value == 8000.0
        assert source == "manual"

    def test_non_numeric_values_are_ignored_without_crashing(self):
        params = {"recoat_time_ms": "not a number", "recoat_time_by_mat": {"steel": None}}
        value, source = resolve_recoat_ms(params, "steel")
        assert value == 9500.0
        assert source == "default"
