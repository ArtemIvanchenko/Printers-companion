"""Shared sort/compare key helpers for timestamps.

Centralises the one safe way to sort heterogeneous timestamp values so the
fix lives in a single place instead of being re-derived (sometimes wrongly)
at each call site.
"""
from __future__ import annotations

from datetime import datetime, timezone


def ts_sort_key(ts: datetime | None) -> float:
    """Return a float sort key for a timestamp, tolerant of None and mixed tz.

    - ``None`` sorts first (``-inf``), preserving the intent of the old
      ``ts or datetime.min`` idiom without its crash.
    - Naive datetimes are treated as UTC, so a collection mixing naive and
      tz-aware values never trips
      "can't compare offset-naive and offset-aware datetimes".
    """
    if ts is None:
        return float("-inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp()
