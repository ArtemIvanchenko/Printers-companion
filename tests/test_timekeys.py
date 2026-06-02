"""Regression tests for the shared tz-safe timestamp sort key.

These lock in the fix for the `ts or datetime.min` idiom that raised
"can't compare offset-naive and offset-aware datetimes" when a collection
mixed None, naive, and tz-aware timestamps (was present in both
reporting/json_report/generator.py and analytics/causal/graph.py).
"""
from datetime import datetime, timezone

from core.utils.timekeys import ts_sort_key


def test_none_sorts_first():
    assert ts_sort_key(None) == float("-inf")


def test_naive_treated_as_utc():
    naive = datetime(2026, 6, 1, 9, 0)
    aware = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert ts_sort_key(naive) == ts_sort_key(aware)


def test_chronological_order_preserved():
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert ts_sort_key(earlier) < ts_sort_key(later)


def test_mixed_naive_aware_none_is_sortable():
    # The old idiom raised TypeError on exactly this mix.
    aware = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 6, 1, 8, 0)
    ordered = sorted([aware, None, naive], key=ts_sort_key)
    assert ordered == [None, naive, aware]
