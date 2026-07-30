"""Per-layer conclusions are stored, and the calibrations read them.

Storing what was concluded from a log instead of re-deriving it from the log is
what makes calibration work against a shared database: the row is visible to
every operator, the file on one operator's disk is not.
"""
from datetime import datetime, timezone

import pytest

from analytics.prediction.layer_timings import store_layer_timings, stored_timings
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


def _time_log_file(layers: dict[int, tuple[float, float]]) -> IngestedFile:
    """A parsed time_log carrying {layer: (burn_ms, pour_ms)}."""
    events = [
        CanonicalEventDraft(
            event_type="layer_timing_summary",
            payload={"layer": layer, "burn_ms": burn, "pour_ms": pour},
        )
        for layer, (burn, pour) in layers.items()
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

    def test_no_time_log_stores_nothing(self, db):
        _session(db, "s4")
        assert store_layer_timings("s4", [], db) == 0


class TestCalibrationsReadTheStoredRows:
    """The point of storing: no file access, so a colleague's print works too."""

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
