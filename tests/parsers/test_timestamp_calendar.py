from datetime import date, datetime
from pathlib import Path

import pytest

from parsers.common.timeline import TimestampQualityTracker
from parsers.common.timestamps import (
    apply_midnight_rollover,
    date_hint_from_filename,
    parse_timestamp_token,
)


@pytest.mark.parametrize("name", ["31.02.2026_error.log", "20260231_stateFlow.log"])
def test_invalid_calendar_filename_is_not_a_date_hint(name):
    assert date_hint_from_filename(Path(name)) is None


@pytest.mark.parametrize("name", ["id120260427_error.log", "id202604271_error.log"])
def test_filename_date_does_not_come_from_inside_a_numeric_id(name):
    assert date_hint_from_filename(Path(name)) is None


@pytest.mark.parametrize("raw", ["2026-02-31 10:00:00", "31.02.2026 10:00:00"])
def test_invalid_explicit_date_does_not_fall_back_to_filename(raw):
    timestamp, raw_timestamp, uncertainty = parse_timestamp_token(raw, date(2026, 4, 27))
    assert timestamp is None
    assert raw_timestamp == raw
    assert uncertainty == 0


@pytest.mark.parametrize("raw", ["24:00:00", "25:00:00", "23:60:00", "23:59:60"])
def test_invalid_clock_fields_remain_unknown(raw):
    assert parse_timestamp_token(raw, date(2026, 4, 27)) == (None, raw, 0)
    full = f"2026-04-27 {raw}"
    assert parse_timestamp_token(full, date(2026, 4, 27)) == (None, full, 0)


def test_clock_fragment_inside_numeric_id_is_not_a_timestamp():
    assert parse_timestamp_token("id1210:00:00", date(2026, 4, 27))[0] is None


@pytest.mark.parametrize("raw", ["10:00:01.000001", "10:00:59,123456"])
def test_hinted_clock_preserves_microseconds_like_explicit_date(raw):
    hinted, raw_timestamp, uncertainty = parse_timestamp_token(raw, date(2026, 4, 27))
    explicit = parse_timestamp_token(f"2026-04-27 {raw}")[0]
    assert hinted == explicit
    assert raw_timestamp == raw
    assert uncertainty == 86400


def test_clock_without_date_hint_preserves_raw_timestamp():
    assert parse_timestamp_token("12:01:02") == (None, "12:01:02", 0)


def test_tracker_preserves_explicit_dates_and_reports_backward_rows():
    tracker = TimestampQualityTracker()
    first = datetime(2026, 4, 28, 23, 59)
    backward = datetime(2026, 4, 27, 0, 1)
    assert tracker.add(first) == first
    assert tracker.add(backward) == backward
    assert tracker.metadata()["midnight_rollovers"] == 0
    assert tracker.metadata()["out_of_order_timestamps"] == 1


def test_explicit_anchor_is_not_shifted_after_clock_rollover():
    tracker = TimestampQualityTracker()
    assert tracker.add(datetime(2026, 4, 27, 23, 59), clock_only=True).day == 27
    assert tracker.add(datetime(2026, 4, 27, 0, 1), clock_only=True).day == 28
    explicit = datetime(2026, 4, 28, 0, 2)
    assert tracker.add(explicit) == explicit
    assert tracker.add(datetime(2026, 4, 27, 0, 3), clock_only=True).day == 28
    assert tracker.metadata()["midnight_rollovers"] == 1


def test_late_explicit_date_anchors_clocks_but_old_row_does_not_rewind_it():
    tracker = TimestampQualityTracker()
    tracker.add(datetime(2026, 4, 30, 23, 59))
    old_row = datetime(2026, 4, 27, 12)
    assert tracker.add(old_row) == old_row
    next_clock = tracker.add(datetime(2026, 4, 27, 0, 1), clock_only=True)
    assert next_clock == datetime(2026, 5, 1, 0, 1)
    assert tracker.metadata()["midnight_rollovers"] == 1


def test_batch_rollover_shares_explicit_date_semantics_with_tracker():
    timestamps = [
        datetime(2026, 4, 27, 23, 59), None,
        datetime(2026, 4, 27, 0, 1), datetime(2026, 4, 28, 0, 2),
    ]
    assert apply_midnight_rollover(timestamps, clock_only=[True, False, True, False]) == [
        datetime(2026, 4, 27, 23, 59), None,
        datetime(2026, 4, 28, 0, 1), datetime(2026, 4, 28, 0, 2),
    ]
    assert apply_midnight_rollover(timestamps) == timestamps


def test_batch_rollover_rejects_mismatched_clock_flags():
    with pytest.raises(ValueError):
        apply_midnight_rollover([datetime(2026, 4, 27)], clock_only=[])
