"""Per-layer conclusions are stored, and the calibrations read them.

Storing what was concluded from a log instead of re-deriving it from the log is
what makes calibration work against a shared database: the row is visible to
every operator, the file on one operator's disk is not.
"""
from datetime import datetime, timezone

import pytest

from analytics.prediction.layer_timings import (
    store_layer_timings,
    stored_layer_cycles,
    stored_layer_overheads,
    stored_timings,
)
from analytics.prediction.recoat_calibration import (
    session_machine_seconds_by_layer,
    session_recoat_seconds,
)
from analytics.prediction.scan_calibration import session_burn_by_layer
from domain.enums.common import DataQualityStatus
from domain.models.events import LayerSnapshot
from domain.models.sessions import BuildSession
from domain.schemas.parsing import CanonicalEventDraft, FileClassification, ParseResult
from domain.services.ingestion import IngestedFile
from storage.db.session import SessionLocal


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def _time_log_file(layers: dict[int, tuple[float, ...]]) -> IngestedFile:
    """A parsed time_log carrying layer -> (burn, pour[, make]) milliseconds."""
    events = [
        CanonicalEventDraft(
            event_type="layer_timing_summary",
            payload={
                "layer": layer,
                "burn_ms": values[0],
                "pour_ms": values[1],
                **({"make_layer_ms": values[2]} if len(values) > 2 else {}),
            },
        )
        for layer, values in layers.items()
    ]
    return IngestedFile(
        path="t_time.log", relative_path="t_time.log",
        classification=FileClassification(
            path="t_time.log", file_name="t_time.log", family="time_log",
            role="secondary", confidence=1.0,
        ),
        checksum="x", size_bytes=1, data_quality_status=DataQualityStatus.ok,
        mtime=datetime.now(timezone.utc),
        parse_result=ParseResult(
            parser_name="time_log", parser_version="0", file_family="time_log",
            role="secondary", events=events,
        ),
    )


def _session(db, session_id: str) -> None:
    db.add(BuildSession(session_id=session_id, classification="REAL_PRINT"))
    db.flush()


class TestStoring:
    def test_parser_contradiction_is_excluded_in_storage(self, db, tmp_path):
        from parsers.base.base import ParserContext
        from parsers.formats.time_log import TimeLogParser

        _session(db, "s_invalid_detail")
        path = tmp_path / "broken_time.log"
        path.write_text(
            "OLD_STATS: 1|9000|30000|39300|\n"
            "NEW_STATS: L1_detailed|Pour_Start:1000|Pour_End:10000|"
            "Burn_Start:10000|Burn_End:60000|MakeLayer_Start:700|Layer_End:60000|\n"
            "OLD_STATS: 2|9000|30000|39300|\n"
        )
        source = _time_log_file({})
        source.parse_result = TimeLogParser().parse(path, ParserContext())
        assert store_layer_timings("s_invalid_detail", [source], db) == 1
        assert stored_timings("s_invalid_detail", db) == {2: (30000.0, 9000.0)}

    def test_invalid_retry_in_another_file_cannot_restore_earlier_attempt(self, db):
        _session(db, "s_invalid_retry")
        good = _time_log_file({1: (30000, 9000, 39300)})
        bad = _time_log_file({1: (30000, 500000, 530300)})
        assert store_layer_timings("s_invalid_retry", [good, bad], db) == 0
        assert stored_timings("s_invalid_retry", db) == {}

    def test_layers_are_stored(self, db):
        _session(db, "s1")
        n = store_layer_timings("s1", [_time_log_file({1: (30000, 9250), 2: (31000, 9300)})], db)

        assert n == 2
        assert stored_timings("s1", db) == {1: (30000.0, 9250.0), 2: (31000.0, 9300.0)}

    def test_reimport_replaces_rather_than_duplicates(self, db):
        """A re-import with fuller logs must not leave the partial read behind."""
        _session(db, "s2")
        store_layer_timings("s2", [_time_log_file({1: (30000, 9250)})], db)
        store_layer_timings(
            "s2", [_time_log_file({1: (30000, 9250), 2: (31000, 9300), 3: (32000, 9100)})], db,
        )

        assert len(stored_timings("s2", db)) == 3
        assert db.query(LayerSnapshot).filter_by(session_id="s2").count() == 3

    def test_stores_valid_interphase_overhead_separately(self, db):
        _session(db, "s_overhead")
        store_layer_timings(
            "s_overhead",
            [_time_log_file({
                1: (30_000, 9_250, 39_500),
                2: (31_000, 9_300, 60_000),  # implausible residual, ignored
            })],
            db,
        )

        assert stored_layer_overheads("s_overhead", db) == {1: pytest.approx(250.0)}
        raw = db.query(LayerSnapshot).filter_by(session_id="s_overhead", layer=2).one()
        assert raw.features["make_layer_ms"] == 60_000.0
        assert "normal_overhead_ms" not in raw.features
        assert stored_layer_cycles("s_overhead", db)[2] == (
            31_000.0, 9_300.0, 60_000.0,
        )

    def test_implausible_readings_are_dropped_at_storage(self, db):
        """The guards the calibrations applied on every read now apply once."""
        _session(db, "s3")
        n = store_layer_timings(
            "s3",
            [_time_log_file({1: (30000, 9250), 2: (0, 9250), 3: (30000, 999999)})],
            db,
        )
        assert n == 1
        assert list(stored_timings("s3", db)) == [1]

    def test_conflicting_repeat_is_excluded_from_calibration_rows(self, db):
        _session(db, "s_conflict")
        source = _time_log_file({
            40: (30_000, 9_250, 39_600),
            41: (31_000, 9_250, 40_600),
        })
        source.parse_result.events.append(CanonicalEventDraft(
            event_type="layer_timing_summary",
            payload={
                "layer": 40,
                "burn_ms": 15_000,
                "pour_ms": 9_250,
                "make_layer_ms": 24_600,
            },
        ))

        assert store_layer_timings("s_conflict", [source], db) == 1
        assert stored_timings("s_conflict", db) == {
            41: (31_000.0, 9_250.0),
        }

    def test_no_time_log_stores_nothing(self, db):
        _session(db, "s4")
        assert store_layer_timings("s4", [], db) == 0


class TestCalibrationsReadTheStoredRows:
    """The point of storing: no file access, so a colleague's print works too."""

    def test_raw_fallback_checks_conflicts_across_daily_files(self, db, monkeypatch):
        from storage.repositories.runtime import RuntimeRepository

        _session(db, "s_daily_conflict")
        files = [
            _time_log_file({1: (30000, 9000, 39300), 2: (31000, 9000, 40300)}),
            _time_log_file({1: (30000, 10000, 40300)}),
        ]
        monkeypatch.setattr(RuntimeRepository, "get_session_files", lambda *a, **kw: files)
        assert session_machine_seconds_by_layer("s_daily_conflict", db) == {2: 40.0}
        assert session_burn_by_layer("s_daily_conflict", db) == {2: 31.0}
        assert session_recoat_seconds("s_daily_conflict", db) == [9.0]

    def test_machine_seconds_come_from_the_database(self, db):
        _session(db, "s5")
        store_layer_timings("s5", [_time_log_file({1: (30000, 9250), 2: (30000, 9250)})], db)

        per_layer = session_machine_seconds_by_layer("s5", db)
        assert per_layer == {1: pytest.approx(39.25), 2: pytest.approx(39.25)}

    def test_burn_seconds_come_from_the_database(self, db):
        _session(db, "s6")
        store_layer_timings("s6", [_time_log_file({1: (30000, 9250)})], db)

        assert session_burn_by_layer("s6", db) == {1: pytest.approx(30.0)}

    def test_recoat_seconds_come_from_the_database(self, db):
        _session(db, "s7")
        store_layer_timings("s7", [_time_log_file({1: (30000, 9250), 2: (30000, 9300)})], db)

        assert sorted(session_recoat_seconds("s7", db)) == [
            pytest.approx(9.25), pytest.approx(9.3),
        ]

    def test_nothing_stored_and_no_files_yields_none(self, db):
        _session(db, "s8")
        assert session_machine_seconds_by_layer("s8", db) is None
        assert session_burn_by_layer("s8", db) is None
        assert session_recoat_seconds("s8", db) is None
