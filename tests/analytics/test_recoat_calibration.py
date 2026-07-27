"""Recoat-time calibration from real per-layer pour_ms in printer logs.

These guard the number that was previously always a hardcoded 9.5s/layer
constant — with layer thickness at 0.06mm and below it dominates the quoted
total time, so getting it from the printer's own measurements instead of a
guess is the single highest-leverage accuracy fix available.
"""
from datetime import datetime, timezone

import pytest

from analytics.prediction.recoat_calibration import (
    RECOAT_MAX_MS,
    RECOAT_MIN_MS,
    _pour_seconds_from_events,
    recalibrate_recoat_and_apply,
    recoat_accuracy,
    session_recoat_seconds,
)
from domain.enums.common import SourceFileFamily
from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession
from domain.schemas.parsing import FileClassification
from domain.services.ingestion import IngestedFile
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


# ── Pure extraction logic — no file I/O ──────────────────────────────────────

def _event(layer, pour_ms, event_type="layer_timing_summary", burn_ms=5000):
    return {"event_type": event_type, "payload": {"layer": layer, "pour_ms": pour_ms, "burn_ms": burn_ms}}


class TestPourSecondsFromEvents:
    def test_extracts_seconds_per_layer(self):
        events = [_event(1, 9500), _event(2, 9600), _event(3, 9400)]
        assert sorted(_pour_seconds_from_events(events)) == [9.4, 9.5, 9.6]

    def test_duplicate_layer_is_first_wins(self):
        # Matches session_overview._layer_burn_times's convention: a rotated
        # or duplicated log must not double-count (or let a later, possibly
        # corrupted, reading silently overwrite a good one).
        events = [_event(1, 9500), _event(1, 500_000)]
        assert _pour_seconds_from_events(events) == [9.5]

    def test_ignores_other_event_types(self):
        events = [_event(1, 9500, event_type="burn_start"), _event(2, 9600)]
        assert _pour_seconds_from_events(events) == [9.6]

    def test_ignores_implausible_readings(self):
        events = [_event(1, 9500), _event(2, 1), _event(3, 10_000_000)]
        assert _pour_seconds_from_events(events) == [9.5]

    def test_ignores_malformed_payload(self):
        events = [
            {"event_type": "layer_timing_summary", "payload": {"layer": None, "pour_ms": 9500}},
            {"event_type": "layer_timing_summary", "payload": {"layer": 1, "pour_ms": "not a number"}},
            {"event_type": "layer_timing_summary", "payload": {}},
        ]
        assert _pour_seconds_from_events(events) == []

    def test_empty_input(self):
        assert _pour_seconds_from_events([]) == []


# ── End-to-end: real time_log file on disk, rehydrated through the real parser ──

def _write_time_log(tmp_path, session_id: str, pour_ms_by_layer: dict, burn_ms: int = 5000, make_ms: int = 15000):
    lines = [f"OLD_STATS: {layer} | {pour} | {burn_ms} | {make_ms} |" for layer, pour in sorted(pour_ms_by_layer.items())]
    # One physical file per session: reusing a path across sessions in the same
    # tmp_path would make every rehydration re-read whichever file was written
    # LAST (rehydration parses from disk at query time, not at write time).
    path = tmp_path / f"{session_id}_time.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _session_with_time_log(db, session_id: str, tmp_path, pour_ms_by_layer: dict, classification: str = "REAL_PRINT"):
    """A BuildSession whose stored payload points at a real on-disk time_log,
    parse_result left unset so RuntimeRepository.get_session_files(rehydrate=True)
    has to actually re-parse it — exercising the real production code path."""
    log_path = _write_time_log(tmp_path, session_id, pour_ms_by_layer)
    ingested = IngestedFile(
        path=str(log_path),
        relative_path=log_path.name,
        classification=FileClassification(
            path=str(log_path), file_name=log_path.name,
            family=SourceFileFamily.time_log, role="secondary", confidence=1.0,
        ),
        checksum="x", size_bytes=log_path.stat().st_size,
        data_quality_status="ok", mtime=datetime.now(timezone.utc),
        parse_result=None,
    )
    RuntimeRepository(db).save_session_payload(
        session_id,
        {"files": [ingested.model_dump(mode="json")], "group": {"classification": classification}},
    )
    row = db.get(BuildSession, session_id)
    row.classification = classification
    row.start_ts = datetime(2027, 1, 1, 8, tzinfo=timezone.utc)


def _record(db, record_id: str, session_id: str, material: str = "steel"):
    db.add(PrintRecord(record_id=record_id, name=record_id, material=material, session_id=session_id))


class TestSessionRecoatSeconds:
    def test_rehydrates_and_extracts_from_a_real_time_log_file(self, db, tmp_path):
        _session_with_time_log(db, "s_rh1", tmp_path, {1: 9500, 2: 9600, 3: 9400})
        db.flush()

        seconds = session_recoat_seconds("s_rh1", db)
        assert sorted(seconds) == [9.4, 9.5, 9.6]

    def test_missing_session_returns_none(self, db):
        assert session_recoat_seconds("s_does_not_exist", db) is None


class TestRecoatAccuracy:
    def test_learns_median_pour_time_per_material(self, db, tmp_path):
        for i, pour_ms in enumerate((9000, 9500, 10_000)):
            sid = f"s_acc{i}"
            _session_with_time_log(db, sid, tmp_path, {1: pour_ms, 2: pour_ms})
            _record(db, f"pr_acc{i}", sid)
        db.flush()

        report = recoat_accuracy(db)
        assert report["n_sessions"] == 3
        assert report["by_material"]["steel"]["n_sessions"] == 3
        # Median of the three session medians (9.0, 9.5, 10.0s) -> 9.5s -> 9500ms
        assert report["by_material"]["steel"]["suggested_recoat_ms"] == pytest.approx(9500.0, abs=1.0)

    def test_non_print_sessions_are_excluded(self, db, tmp_path):
        _session_with_time_log(db, "s_svc", tmp_path, {1: 9500}, classification="SERVICE_SESSION")
        _record(db, "pr_svc", "s_svc")
        db.flush()

        report = recoat_accuracy(db)
        assert report["n_usable_sessions"] == 0
        assert report["by_material"] == {}
        assert report["excluded"][0]["reason"] == "not_a_print"

    def test_session_without_time_log_is_silently_skipped(self, db):
        # No files at all -> get_session_files returns None -> no crash, no row.
        db.add(BuildSession(session_id="s_notime", status="x", classification="REAL_PRINT",
                            context={"runtime_payload": {"files": [], "group": {}}},
                            start_ts=datetime(2027, 2, 1, tzinfo=timezone.utc)))
        _record(db, "pr_notime", "s_notime")
        db.flush()

        report = recoat_accuracy(db)
        assert report["n_sessions"] == 0


class TestRecalibrateRecoatAndApply:
    def test_applies_learned_value_per_material(self, db, tmp_path):
        db.add(MachineParams(id=1, hatch_speed_mm_s=800, laser_count=1))
        for i in range(3):
            sid = f"s_cal{i}"
            _session_with_time_log(db, sid, tmp_path, {1: 12_000, 2: 12_000})
            _record(db, f"pr_cal{i}", sid)
        db.flush()

        result = recalibrate_recoat_and_apply(db)
        assert result["applied"]["steel"] == pytest.approx(12_000.0, abs=1.0)
        assert db.get(MachineParams, 1).recoat_time_by_mat["steel"] == pytest.approx(12_000.0, abs=1.0)

    def test_out_of_range_value_is_surfaced_not_applied(self, db, tmp_path):
        db.add(MachineParams(id=1, hatch_speed_mm_s=800, laser_count=1))
        for i in range(3):
            sid = f"s_bad{i}"
            # 90s/layer recoat is not physically plausible -> RECOAT_MAX_MS guard
            _session_with_time_log(db, sid, tmp_path, {1: 90_000})
            _record(db, f"pr_bad{i}", sid)
        db.flush()

        result = recalibrate_recoat_and_apply(db)
        assert result["applied"] == {}
        assert result["skipped"][0]["reason"] == "out_of_range"
        assert result["skipped"][0]["recoat_ms"] > RECOAT_MAX_MS

    def test_locked_params_are_never_overwritten(self, db, tmp_path):
        db.add(MachineParams(id=1, correction_locked=True, recoat_time_by_mat={"steel": 8000.0}))
        for i in range(3):
            sid = f"s_lock{i}"
            _session_with_time_log(db, sid, tmp_path, {1: 15_000})
            _record(db, f"pr_lock{i}", sid)
        db.flush()

        result = recalibrate_recoat_and_apply(db)
        assert result["locked"] is True
        assert result["applied"] == {}
        assert db.get(MachineParams, 1).recoat_time_by_mat == {"steel": 8000.0}

    def test_below_minimum_sessions_nothing_is_learned(self, db, tmp_path):
        db.add(MachineParams(id=1, hatch_speed_mm_s=800, laser_count=1))
        _session_with_time_log(db, "s_few", tmp_path, {1: 9500})
        _record(db, "pr_few", "s_few")
        db.flush()

        assert recalibrate_recoat_and_apply(db)["applied"] == {}

    def test_bounds_sanity(self):
        assert RECOAT_MIN_MS < RECOAT_MAX_MS
