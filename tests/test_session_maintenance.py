"""Legacy repairs retain raw processing without holding SQL or overwriting peers."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib

import pytest
from sqlalchemy import event

from core.config.settings import get_settings
from domain.models.sessions import BuildSession
from domain.schemas.parsing import FileClassification
from domain.services.ingestion import IngestedFile
from domain.services import session_sources
from scripts import backfill_layer_timings
from scripts.maintenance import backfill_session_overview as overview
from scripts.maintenance import recompute_session_times as times
from storage.db.session import SessionLocal, engine


def _legacy(tmp_path, sid="legacy", *, owner=None, context=None):
    path = tmp_path / f"{sid}_time.log"
    path.write_text("OLD_STATS: 1|9000|30000|39300|\n", encoding="utf-8")
    file = IngestedFile(path=str(path), relative_path=path.name,
                        classification=FileClassification(path=str(path), file_name=path.name,
                                                          family="time_log", role="secondary", confidence=1),
                        checksum=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size,
                        data_quality_status="ok", mtime=datetime.now(timezone.utc))
    payload = {"files": [file.model_dump(mode="json")], "group": {}}
    with SessionLocal() as db:
        db.add(BuildSession(session_id=sid, origin_compute_node_id=owner or get_settings().compute_node_id,
                            context=context or {"runtime_payload": payload}))
        db.commit()
    return payload


@pytest.fixture
def no_sql_during_sources(monkeypatch):
    active = set()

    def checkout(connection, record, proxy):
        active.add(id(record))

    def checkin(connection, record):
        active.discard(id(record))

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    original = session_sources.rehydrate_parse_results

    def guarded(*args, **kwargs):
        assert not active, "raw parsing retained a SQL connection"
        return original(*args, **kwargs)

    monkeypatch.setattr(session_sources, "rehydrate_parse_results", guarded)
    yield active
    event.remove(engine, "checkout", checkout)
    event.remove(engine, "checkin", checkin)


def test_timing_backfill_parses_after_read_close_and_remains_idempotent(tmp_path, no_sql_during_sources):
    from analytics.prediction.layer_timings import stored_timings

    _legacy(tmp_path)
    assert backfill_layer_timings.backfill(dry_run=True) == 1
    with SessionLocal() as db:
        assert "timing_publication" not in db.get(BuildSession, "legacy").context
    assert backfill_layer_timings.backfill(dry_run=False) == 1
    with SessionLocal() as db:
        assert stored_timings("legacy", db) == {1: (30000.0, 9000.0)}
    assert backfill_layer_timings.backfill(dry_run=False) == 0


def test_timing_only_legacy_can_repair_overview_and_times_without_replacing_measurements(tmp_path, no_sql_during_sources):
    from sqlalchemy import select
    from domain.models.events import LayerSnapshot

    _legacy(tmp_path)
    assert backfill_layer_timings.backfill(dry_run=False) == 1

    def timings():
        with SessionLocal() as db:
            manifest = deepcopy(db.get(BuildSession, "legacy").context["timing_publication"])
            rows = [tuple(row) for row in db.execute(select(
                LayerSnapshot.layer_snapshot_id, LayerSnapshot.layer,
                LayerSnapshot.features, LayerSnapshot.context,
            ).where(LayerSnapshot.session_id == "legacy"))]
            return manifest, rows

    initial = timings()
    assert initial[0]["source"] == {}
    overview.backfill(dry_run=False)
    assert timings() == initial
    with SessionLocal() as db:
        group = db.get(BuildSession, "legacy").context["runtime_payload"]["group"]
        assert group["features"]["layers"] == 1
        assert "analysis_snapshot" not in group
    assert times.recompute(dry_run=False) == 1
    assert timings() == initial
    overview.backfill(dry_run=False, force=True)
    assert timings() == initial
    assert backfill_layer_timings.backfill(dry_run=False) == 0


def test_timing_with_original_import_source_uses_reanalysis_not_legacy_repair(tmp_path, monkeypatch):
    payload = _legacy(tmp_path)
    with SessionLocal() as db:
        row = db.get(BuildSession, "legacy")
        row.context = {"runtime_payload": payload, "timing_publication": {
            "source": {"import_job_id": "original-import"},
        }}
        db.commit()
    calls = []

    def request(db, sid, **kwargs):
        calls.append(sid)
        return {"job_status": "checking_stability", "job_id": "original-import"}

    monkeypatch.setattr("domain.services.session_requests.request_analysis", request)
    monkeypatch.setattr(session_sources, "rehydrate_parse_results", lambda *args: pytest.fail("modern raw IO"))
    overview.backfill(dry_run=False)
    assert times.recompute(dry_run=False) == 0
    assert calls == ["legacy", "legacy"]
    with SessionLocal() as db:
        assert db.get(BuildSession, "legacy").context["runtime_payload"] == payload


@pytest.mark.parametrize("manifest", [{"status": "empty"}, {"row_count": 1, "rows_fingerprint": "wrong"}])
def test_authoritative_empty_or_invalid_timing_is_not_replaced(tmp_path, manifest, monkeypatch):
    payload = _legacy(tmp_path)
    with SessionLocal() as db:
        row = db.get(BuildSession, "legacy")
        row.context = {"runtime_payload": payload, "timing_publication": manifest}
        db.commit()
    monkeypatch.setattr(session_sources, "rehydrate_parse_results", lambda *args: pytest.fail("raw fallback"))
    assert backfill_layer_timings.backfill(dry_run=False) == 0
    with SessionLocal() as db:
        assert db.get(BuildSession, "legacy").context["timing_publication"] == manifest


def test_foreign_owner_never_opens_raw_sources(tmp_path, monkeypatch):
    _legacy(tmp_path, owner="another-pc")
    monkeypatch.setattr(session_sources, "rehydrate_parse_results", lambda *args: pytest.fail("foreign raw IO"))
    assert backfill_layer_timings.backfill(dry_run=False) == 0
    overview.backfill(dry_run=False, force=True)
    assert times.recompute(dry_run=False) == 0


def test_legacy_overview_computes_without_sql_and_rejects_concurrent_change(tmp_path, no_sql_during_sources, monkeypatch):
    _legacy(tmp_path)

    def calculate(*args, **kwargs):
        assert not no_sql_during_sources
        with SessionLocal() as db:
            row = db.get(BuildSession, "legacy")
            row.context = {**row.context, "operator_note": "new"}
            db.commit()
        return {"classification": "REAL_PRINT", "features": {"layers": 1}}

    monkeypatch.setattr("domain.services.session_overview.build_group_overview", calculate)
    overview.backfill(dry_run=False)
    with SessionLocal() as db:
        context = db.get(BuildSession, "legacy").context
        assert context["operator_note"] == "new"
        assert context["runtime_payload"]["group"] == {}


def test_modern_repair_queues_original_job_without_parsing(tmp_path, monkeypatch, capsys):
    payload = _legacy(tmp_path)
    with SessionLocal() as db:
        row = db.get(BuildSession, "legacy")
        payload["group"] = {"analysis_snapshot": {"analysis_id": "saved"}, "features": {"layers": 1}}
        row.context = {"runtime_payload": payload}
        db.commit()
    calls = []

    def request(db, sid, **kwargs):
        calls.append((sid, kwargs["compute_node_id"]))
        return {"job_status": "importing", "job_id": "original-job"}

    monkeypatch.setattr("domain.services.session_requests.request_analysis", request)
    monkeypatch.setattr(session_sources, "rehydrate_parse_results", lambda *args: pytest.fail("modern raw IO"))
    overview.backfill(dry_run=False, force=False)
    assert calls == []
    overview.backfill(dry_run=True, force=True)
    assert calls == []
    overview.backfill(dry_run=False, force=True)
    assert calls == [("legacy", get_settings().compute_node_id)]
    assert times.recompute(dry_run=False) == 0
    assert len(calls) == 2
    assert "JOB importing" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.get(BuildSession, "legacy").context["runtime_payload"] == payload


def test_legacy_overview_force_remains_repeatable_without_modern_marker(tmp_path, no_sql_during_sources, monkeypatch):
    _legacy(tmp_path)
    calls = []

    def calculate(*args, **kwargs):
        assert not no_sql_during_sources
        calls.append(1)
        return {"features": {"layers": len(calls)}, "analysis_snapshot": {"analysis_id": "unpublished"}}

    monkeypatch.setattr("domain.services.session_overview.build_group_overview", calculate)
    overview.backfill(dry_run=False)
    overview.backfill(dry_run=False, force=True)
    assert len(calls) == 2
    with SessionLocal() as db:
        group = db.get(BuildSession, "legacy").context["runtime_payload"]["group"]
        assert "analysis_snapshot" not in group
        assert group["features"]["layers"] == 2


def test_modern_repair_missing_source_job_reports_conflict_without_changes(tmp_path, monkeypatch, capsys):
    payload = _legacy(tmp_path)
    payload["group"] = {"analysis_snapshot": {"analysis_id": "saved"}}
    with SessionLocal() as db:
        row = db.get(BuildSession, "legacy")
        row.context = {"runtime_payload": payload}
        db.commit()
    monkeypatch.setattr(session_sources, "rehydrate_parse_results", lambda *args: pytest.fail("modern raw IO"))
    assert times.recompute(dry_run=False) == 0
    assert "нет исходного задания" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.get(BuildSession, "legacy").context["runtime_payload"] == payload


def test_legacy_times_keep_sensor_statistics_and_preserve_dry_run(tmp_path, no_sql_during_sources, monkeypatch):
    payload = _legacy(tmp_path)
    payload["group"] = {"signal_stats": {"gas": {"mean": 7}}, "features": {"duration_min": 100}}
    with SessionLocal() as db:
        row = db.get(BuildSession, "legacy")
        row.context = {"runtime_payload": payload}
        db.commit()
        before = deepcopy(row.context)

    def calculate(*args, **kwargs):
        assert not no_sql_during_sources
        return {"features": {"duration_min": 5}, "signal_stats": {},
                "analysis_snapshot": {"analysis_id": "unpublished"},
                "start_ts": "2026-01-01T01:00:00+00:00", "end_ts": "2026-01-01T01:05:00+00:00"}

    monkeypatch.setattr(times, "build_group_overview", calculate)
    assert times.recompute(dry_run=True) == 1
    with SessionLocal() as db:
        assert db.get(BuildSession, "legacy").context == before
    assert times.recompute(dry_run=False) == 1
    with SessionLocal() as db:
        row = db.get(BuildSession, "legacy")
        group = row.context["runtime_payload"]["group"]
        assert group["signal_stats"] == payload["group"]["signal_stats"]
        assert group["features"]["duration_min"] == 5
        assert "analysis_snapshot" not in group
        assert row.start_ts is not None
    assert times.recompute(dry_run=False) == 1
