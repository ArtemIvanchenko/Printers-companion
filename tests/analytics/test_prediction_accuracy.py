"""Predicted-vs-actual accuracy report and auto-calibration.

These guard the numbers the operator quotes to a customer: the correction
factor scales every predicted print time, and the cost estimate is derived
from that time.
"""
from datetime import datetime, timezone

import pytest

from analytics.prediction.accuracy import (
    CORRECTION_MAX,
    MIN_PAIRS_FOR_CALIBRATION,
    prediction_accuracy,
    recalibrate_and_apply,
)
from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def _session(
    db, session_id: str, start: datetime, hours: float, classification: str = "REAL_PRINT"
) -> None:
    from datetime import timedelta

    db.add(
        BuildSession(
            session_id=session_id,
            status="runtime_payload",
            classification=classification,
            start_ts=start,
            end_ts=start + timedelta(hours=hours),
            context={"runtime_payload": {"group": {"classification": classification}}},
        )
    )


def _record(
    db, record_id: str, session_id: str, raw_hours: float,
    material: str = "steel", factor: float = 1.0, printed_at: datetime | None = None,
) -> None:
    db.add(
        PrintRecord(
            record_id=record_id,
            name=record_id,
            material=material,
            session_id=session_id,
            printed_at=printed_at,
            metadata_json={
                "prediction": {
                    "raw_print_hours": raw_hours,
                    "print_hours": raw_hours * factor,
                    "correction_factor": factor,
                    "material": material,
                    "estimated_at": "2027-01-01T00:00:00+00:00",
                }
            },
        )
    )


class TestPredictionAccuracy:
    def test_error_pct_is_measured_against_the_figure_the_operator_saw(self, db):
        # Raw estimate 10 h, correction ×1.2 → the operator was quoted 12 h.
        # Actual came out at 12 h, so the quote was spot on even though the
        # uncorrected geometric number was 17% low.
        start = datetime(2027, 4, 1, 8, 0, tzinfo=timezone.utc)
        _session(db, "s_acc1", start, hours=12.0)
        _record(db, "pr_acc1", "s_acc1", raw_hours=10.0, factor=1.2)
        db.flush()

        row = prediction_accuracy(db)["pairs"][0]
        assert row["predicted_hours"] == 12.0
        assert row["error_pct"] == 0.0
        # The raw figure is still reported — it is what the ratio is built from.
        assert row["raw_predicted_hours"] == 10.0
        assert row["raw_error_pct"] == pytest.approx(-16.7, abs=0.1)

    def test_calibration_window_takes_the_most_recent_prints(self, db):
        """The window must be by print date, not by row order.

        20 recent prints that ran exactly on estimate and 20 stale ones that ran
        2× over. They are inserted recent-first, so row order is the OPPOSITE of
        chronology: slicing the list (as the old code did) picks the stale pairs
        and learns ×2, while a date-ordered window correctly learns ×1.
        """
        for i in range(20):
            day = datetime(2027, 6, 1, 8, 0, tzinfo=timezone.utc).replace(day=i % 28 + 1)
            _session(db, f"s_new{i}", day, hours=10.0)
            _record(db, f"pr_new{i}", f"s_new{i}", raw_hours=10.0)
        for i in range(20):
            day = datetime(2027, 1, 1, 8, 0, tzinfo=timezone.utc).replace(day=i % 28 + 1)
            _session(db, f"s_old{i}", day, hours=20.0)
            _record(db, f"pr_old{i}", f"s_old{i}", raw_hours=10.0)
        db.flush()

        factor = prediction_accuracy(db)["by_material"]["steel"]["suggested_factor"]
        assert factor == pytest.approx(1.0, abs=0.01), (
            f"window ignored print dates and learned {factor} from stale pairs"
        )

    def test_pairs_are_returned_newest_first(self, db):
        base = datetime(2027, 11, 1, 8, 0, tzinfo=timezone.utc)
        _session(db, "s_ord_old", base, hours=10.0)
        _record(db, "pr_ord_old", "s_ord_old", raw_hours=10.0)
        _session(db, "s_ord_new", base.replace(day=20), hours=10.0)
        _record(db, "pr_ord_new", "s_ord_new", raw_hours=10.0)
        db.flush()

        pairs = prediction_accuracy(db)["pairs"]
        assert [p["record_id"] for p in pairs] == ["pr_ord_new", "pr_ord_old"]

    def test_non_print_sessions_never_train_the_factor(self, db):
        # A service session's span is not a print duration; using it would drag
        # the factor toward nonsense.
        start = datetime(2027, 5, 1, 8, 0, tzinfo=timezone.utc)
        for i in range(MIN_PAIRS_FOR_CALIBRATION):
            _session(db, f"s_svc{i}", start.replace(day=i + 1), hours=0.5,
                     classification="SERVICE_SESSION")
            _record(db, f"pr_svc{i}", f"s_svc{i}", raw_hours=10.0)
        db.flush()

        report = prediction_accuracy(db)
        assert report["n_usable_pairs"] == 0
        assert report["by_material"] == {}
        assert all(e["reason"] == "not_a_print" for e in report["excluded"])
        assert all(r["used_for_calibration"] is False for r in report["pairs"])

    def test_implausible_span_is_reported_but_not_used(self, db):
        start = datetime(2027, 5, 1, 8, 0, tzinfo=timezone.utc)
        _session(db, "s_huge", start, hours=24 * 40)  # 40 days — mis-grouped
        _record(db, "pr_huge", "s_huge", raw_hours=10.0)
        db.flush()

        report = prediction_accuracy(db)
        assert report["n_pairs"] == 1
        assert report["n_usable_pairs"] == 0
        assert report["excluded"][0]["reason"] == "implausible_duration"


class TestRecalibration:
    def test_applies_median_ratio_per_material(self, db):
        db.add(MachineParams(id=1, hatch_speed_mm_s=800, laser_count=1))
        start = datetime(2027, 7, 1, 8, 0, tzinfo=timezone.utc)
        for i, actual in enumerate((11.0, 12.0, 13.0)):
            _session(db, f"s_cal{i}", start.replace(day=i + 1), hours=actual)
            _record(db, f"pr_cal{i}", f"s_cal{i}", raw_hours=10.0)
        db.flush()

        result = recalibrate_and_apply(db)
        assert result["applied"]["steel"] == pytest.approx(1.2, abs=0.001)
        assert db.get(MachineParams, 1).time_correction_by_mat["steel"] == pytest.approx(1.2, abs=0.001)

    def test_out_of_range_factor_is_surfaced_not_applied(self, db):
        db.add(MachineParams(id=1, hatch_speed_mm_s=800, laser_count=1))
        start = datetime(2027, 8, 1, 8, 0, tzinfo=timezone.utc)
        for i in range(MIN_PAIRS_FOR_CALIBRATION):
            # actual 5× the estimate → parameters are wrong, not the calibration
            _session(db, f"s_bad{i}", start.replace(day=i + 1), hours=50.0)
            _record(db, f"pr_bad{i}", f"s_bad{i}", raw_hours=10.0)
        db.flush()

        result = recalibrate_and_apply(db)
        assert result["applied"] == {}
        assert result["skipped"][0]["reason"] == "out_of_range"
        assert result["skipped"][0]["factor"] > CORRECTION_MAX

    def test_locked_factors_are_never_overwritten(self, db):
        db.add(MachineParams(id=1, correction_locked=True, time_correction_by_mat={"steel": 1.5}))
        start = datetime(2027, 9, 1, 8, 0, tzinfo=timezone.utc)
        for i in range(MIN_PAIRS_FOR_CALIBRATION):
            _session(db, f"s_lock{i}", start.replace(day=i + 1), hours=11.0)
            _record(db, f"pr_lock{i}", f"s_lock{i}", raw_hours=10.0)
        db.flush()

        result = recalibrate_and_apply(db)
        assert result["locked"] is True
        assert result["applied"] == {}
        assert db.get(MachineParams, 1).time_correction_by_mat == {"steel": 1.5}

    def test_below_minimum_pairs_nothing_is_learned(self, db):
        db.add(MachineParams(id=1, hatch_speed_mm_s=800, laser_count=1))
        start = datetime(2027, 10, 1, 8, 0, tzinfo=timezone.utc)
        _session(db, "s_few", start, hours=12.0)
        _record(db, "pr_few", "s_few", raw_hours=10.0)
        db.flush()

        assert recalibrate_and_apply(db)["applied"] == {}
