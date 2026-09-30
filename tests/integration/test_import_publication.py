"""Atomic compact results, stale workers, explicit empty evidence and boundaries."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from analytics.prediction.calibration_inputs import load_calibration_inputs
from analytics.prediction.layer_timings import (
    store_layer_timings,
    stored_timing_events,
    stored_timings,
)
from analytics.prediction.timing_snapshot import (
    MANIFEST_KEY,
    prepare_layer_timings,
    published_timing_events,
    timing_publication_status,
)
from core.config.settings import get_settings
from domain.enums.common import ImportJobStatus
from domain.models.events import LayerSnapshot
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact
from domain.schemas.parsing import CanonicalEventDraft, FileClassification, ParseResult
from domain.services.importing.contracts import (
    ImportExecutionResult,
    ImportJobRecord,
    ImportPersistenceError,
)
from domain.services.importing.fence import ImportFence, StaleImportLeaseError
from domain.services.importing.publication import (
    ImportPublicationConflict,
    prepare_import_reports,
    publish_import,
    session_publication_token,
)
from domain.services.ingestion import IngestedFile
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def _file(burn=30_000):
    return IngestedFile(
        path="t_time.log",
        relative_path="t_time.log",
        checksum="a" * 64,
        size_bytes=1,
        data_quality_status="ok",
        mtime=datetime.now(timezone.utc),
        classification=FileClassification(
            path="t_time.log",
            file_name="t_time.log",
            family="time_log",
            role="secondary",
            confidence=1,
        ),
        parse_result=ParseResult(
            parser_name="time_log",
            parser_version="1",
            file_family="time_log",
            role="secondary",
            events=[
                CanonicalEventDraft(
                    event_type="layer_timing_summary",
                    payload={
                        "layer": 1,
                        "burn_ms": burn,
                        "pour_ms": 9000,
                        "make_layer_ms": burn + 9200,
                    },
                )
            ],
        ),
    )


@pytest.fixture
def attempt(monkeypatch):
    monkeypatch.setattr(get_settings(), "compute_node_id", "pub-node")
    job = ImportJobRecord(
        import_job_id="import-pub",
        owner_node_id="pub-node",
        source_path="/local/logs",
        source_name="logs",
        status=ImportJobStatus.checking_stability,
        lease_owner="pub-worker",
        lease_generation=2,
        lease_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    fence = ImportFence(job.import_job_id, job.owner_node_id, job.lease_owner, job.lease_generation)
    sid = "pub-session"
    with SessionLocal() as db:
        db.add(
            BuildSession(
                session_id=sid,
                origin_compute_node_id=job.owner_node_id,
                context={"runtime_payload": {"files": [], "group": {"marker": "old"}}},
            )
        )
        db.flush()
        store_layer_timings(sid, [_file(20_000)], db)
        RuntimeRepository(db).save_import_job(job)
        db.add(
            PrintRecord(
                record_id="pub-card",
                name="Existing card",
                session_id=sid,
                origin_compute_node_id=job.owner_node_id,
            )
        )
        db.commit()
    with SessionLocal() as db:
        token = session_publication_token(db.get(BuildSession, sid))
    prepared = prepare_layer_timings(
        [_file()], owner_node_id=job.owner_node_id, source=fence.as_dict()
    )
    job.status = ImportJobStatus.done
    job.session_ids = [sid]
    job.report_ids = ["pub-report"]
    result = ImportExecutionResult(
        job=job,
        previous_session_tokens={sid: token},
        layer_timings={sid: prepared},
        sessions={
            sid: {
                "files": [],
                "group": {
                    "classification": "REAL_PRINT",
                    "marker": "new",
                    "timing_publication_id": prepared.manifest["publication_id"],
                },
            }
        },
        reports={
            "pub-report": {
                "report_id": "pub-report",
                "session_id": sid,
                "timing_publication": prepared.manifest,
                "timeline": [],
            }
        },
    )
    return result, fence


def _assert_old():
    with SessionLocal() as db:
        assert stored_timings("pub-session", db) == {1: (20_000.0, 9000.0)}
        assert (
            db.get(BuildSession, "pub-session").context["runtime_payload"]["group"]["marker"]
            == "old"
        )
        assert db.get(ReportArtifact, "pub-report") is None
        assert db.get(ImportJob, "import-pub").status != "done"


@pytest.mark.parametrize("corrupt", ["report", "prepared_payload"])
def test_mismatched_analysis_snapshot_rolls_back_every_publication(attempt, corrupt):
    result, fence = attempt
    snapshot = {"schema_version": 1, "analysis_id": "expected"}
    result.sessions["pub-session"]["group"]["analysis_snapshot"] = deepcopy(snapshot)
    result.reports["pub-report"]["analysis_snapshot"] = deepcopy(snapshot)
    reports = prepare_import_reports(result)
    target = result.reports["pub-report"] if corrupt == "report" else reports["pub-report"]["payload"]
    target["analysis_snapshot"] = {**snapshot, "analysis_id": "wrong"}
    with pytest.raises(ValueError, match="аналитическим снимкам"), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    _assert_old()


def test_preparation_has_no_visible_changes_then_publication_is_complete(attempt):
    result, fence = attempt
    reports = prepare_import_reports(result)
    _assert_old()
    with SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    with SessionLocal() as db:
        assert stored_timings("pub-session", db) == {1: (30_000.0, 9000.0)}
        session = db.get(BuildSession, "pub-session")
        publication = session.context[MANIFEST_KEY]
        assert (
            publication["publication_id"]
            == session.context["runtime_payload"]["group"]["timing_publication_id"]
        )
        assert db.get(ReportArtifact, "pub-report").payload[MANIFEST_KEY] == publication
        assert db.get(ImportJob, "import-pub").status == "done"
        assert db.get(ImportJob, "import-pub").lease_owner is None
        assert load_calibration_inputs(db).burns["pub-session"] == {1: 30.0}


@pytest.mark.parametrize(
    "field,value",
    [
        ("lease_generation", 3),
        ("lease_owner", "replacement-worker"),
        ("owner_node_id", "another-PC"),
        ("status", "ignored"),
        ("lease_until", datetime(2000, 1, 1, tzinfo=timezone.utc)),
    ],
)
def test_stale_attempt_cannot_replace_any_result(attempt, field, value):
    result, fence = attempt
    reports = prepare_import_reports(result)
    with SessionLocal() as db:
        setattr(db.get(ImportJob, "import-pub"), field, value)
        db.commit()
    with pytest.raises(StaleImportLeaseError), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    _assert_old()


def test_postponed_claim_is_a_valid_fence(attempt):
    result, fence = attempt
    with SessionLocal() as db:
        db.get(ImportJob, "import-pub").status = "postponed"
        db.commit()
    with SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=prepare_import_reports(result))
        db.commit()


def test_late_expiry_rolls_back_layers_overview_report_and_job(attempt, monkeypatch):
    result, fence = attempt
    reports = prepare_import_reports(result)
    original = RuntimeRepository.save_prepared_report

    def expire_after_report(repo, report_id, prepared):
        original(repo, report_id, prepared)
        repo.db.get(ImportJob, "import-pub").lease_until = datetime(2000, 1, 1, tzinfo=timezone.utc)
        repo.db.flush()

    monkeypatch.setattr(RuntimeRepository, "save_prepared_report", expire_after_report)
    with pytest.raises(StaleImportLeaseError), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    _assert_old()


def test_report_write_failure_rolls_back_earlier_updates(attempt, monkeypatch):
    result, fence = attempt
    reports = prepare_import_reports(result)

    def fail(*args):
        raise RuntimeError("simulated disk/SQL failure")

    monkeypatch.setattr(RuntimeRepository, "save_prepared_report", fail)
    with pytest.raises(RuntimeError, match="simulated"), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    _assert_old()


def test_other_job_changed_the_same_session_requires_reprepare(attempt):
    result, fence = attempt
    with SessionLocal() as db:
        row = db.get(BuildSession, "pub-session")
        row.context = {**row.context, "newer_import": "committed"}
        db.commit()
    with pytest.raises(ImportPublicationConflict), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=prepare_import_reports(result))
        db.commit()
    _assert_old()


@pytest.mark.parametrize("files,status", [([], "no_time_log"), ([_file(0)], "empty")])
def test_explicit_empty_replaces_old_layers_without_raw_fallback(
    attempt, files, status, monkeypatch
):
    result, fence = attempt
    sid = "pub-session"
    prepared = prepare_layer_timings(files, source=fence.as_dict())
    result.layer_timings[sid] = prepared
    result.sessions[sid]["group"]["timing_publication_id"] = prepared.manifest["publication_id"]
    result.reports["pub-report"][MANIFEST_KEY] = prepared.manifest
    with SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=prepare_import_reports(result))
        db.commit()
    from analytics.prediction.scan_calibration import session_burn_by_layer

    def no_fallback(*args, **kwargs):
        raise AssertionError("must not reopen raw logs")

    monkeypatch.setattr(RuntimeRepository, "get_session_files", no_fallback)
    with SessionLocal() as db:
        assert stored_timing_events(sid, db) == []
        assert db.get(BuildSession, sid).context[MANIFEST_KEY]["status"] == status
        assert not session_burn_by_layer(sid, db)
        assert load_calibration_inputs(db).events[sid] == []
        RuntimeRepository(db).save_session_payload(sid, {"files": [], "group": {}})
        assert stored_timing_events(sid, db) == []


@pytest.mark.parametrize(
    "corruption", ["tag", "count", "value", "missing_manifest", "negative_layer"]
)
def test_damaged_snapshot_is_excluded_by_all_shared_consumers(attempt, corruption):
    with SessionLocal() as db:
        row = db.scalar(select(LayerSnapshot).where(LayerSnapshot.session_id == "pub-session"))
        session = db.get(BuildSession, "pub-session")
        if corruption == "tag":
            row.context = {"publication_id": "other-generation"}
        elif corruption == "value":
            row.features = {**row.features, "burn_ms": 900_000}
        elif corruption == "negative_layer":
            row.layer = -1
        elif corruption == "count":
            session.context = {
                **session.context,
                MANIFEST_KEY: {**session.context[MANIFEST_KEY], "row_count": 2},
            }
        else:
            session.context = {
                key: value for key, value in session.context.items() if key != MANIFEST_KEY
            }
        db.commit()
    with SessionLocal() as db:
        assert stored_timing_events("pub-session", db) == []
        assert load_calibration_inputs(db).events["pub-session"] == []
    from domain.services.log_insights import print_log_insights

    report = print_log_insights("pub-card")
    assert report["status"] == "needs_reanalysis"
    assert report["timing_publication_status"] == "invalid"


def test_unparsed_daily_file_cannot_replace_complete_timings(attempt):
    source = _file()
    source.parse_result = None
    with pytest.raises(ValueError, match="Не все файлы"):
        prepare_layer_timings([_file(), source])
    _assert_old()


def test_legacy_malformed_rows_are_not_mistaken_for_missing_evidence():
    events = published_timing_events([(None, {}, None), (1, {}, None)], None)
    assert events is not None and len(events) == 2
    assert published_timing_events([], None) is None
    assert timing_publication_status([(1, {}, "new-generation")], None) == "invalid"


def test_new_session_base_token_survives_sqlite_timezone_roundtrip(attempt):
    from domain.services.importing.persistence import _ensure_session_record

    _, fence = attempt
    token = _ensure_session_record(
        "new-base", 1.0, origin_compute_node_id=fence.owner_node_id, fence=fence
    )
    with SessionLocal() as db:
        assert token == session_publication_token(db.get(BuildSession, "new-base"))


def test_source_event_batch_checks_authoritative_fence_even_with_true_cached_guard(attempt):
    from domain.services.importing.persistence import persist_parse_results_to_db

    _, fence = attempt
    with SessionLocal() as db:
        db.get(ImportJob, "import-pub").lease_generation += 1
        db.commit()
    with pytest.raises(StaleImportLeaseError):
        persist_parse_results_to_db("pub-session", [_file()], fence=fence, lease_guard=lambda: True)
    _assert_old()


def test_report_preparation_runs_outside_sql_transaction(attempt, monkeypatch):
    result, fence = attempt
    from sqlalchemy import event
    from storage.db.session import engine

    checked_out = set()

    def checkout(connection, record, proxy):
        checked_out.add(id(connection))

    def checkin(connection, record):
        checked_out.discard(id(connection))

    def upload(report_id, report):
        assert not checked_out
        return f"s3://reports/{report_id}/checksum.json"

    monkeypatch.setattr("storage.repositories.runtime._offload_report", upload)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        reports = prepare_import_reports(result)
        with SessionLocal() as db:
            publish_import(db, result, fence=fence, prepared_reports=reports)
            db.commit()
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_production_cannot_finish_without_full_report_archived(attempt, monkeypatch):
    result, _ = attempt
    monkeypatch.setattr(get_settings(), "app_env", "production")
    monkeypatch.setattr("storage.repositories.runtime._offload_report", lambda *args: None)
    with pytest.raises(ImportPersistenceError, match="отчёт"):
        prepare_import_reports(result)
    _assert_old()


def test_incomplete_import_never_links_old_sessions_or_publishes_analysis(attempt):
    result, fence = attempt
    postponed = ImportExecutionResult(job=result.job.model_copy(deep=True))
    postponed.job.status = ImportJobStatus.postponed
    with SessionLocal() as db:
        publish_import(db, postponed, fence=fence, prepared_reports={})
        db.commit()
    _assert_old()


def test_report_from_another_generation_rejects_whole_publication(attempt):
    result, fence = attempt
    result.reports = deepcopy(result.reports)
    result.reports["pub-report"][MANIFEST_KEY]["publication_id"] = "wrong-generation"
    with pytest.raises(ValueError, match="Отчёт"), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=prepare_import_reports(result))
        db.commit()
    _assert_old()


def test_uploaded_report_from_another_generation_is_rejected(attempt):
    result, fence = attempt
    reports = prepare_import_reports(result)
    reports["pub-report"]["payload"][MANIFEST_KEY] = {"publication_id": "other"}
    with pytest.raises(ValueError, match="Отчёт"), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    _assert_old()


def test_partial_multi_session_failure_preserves_all_previous_results(attempt, monkeypatch):
    result, fence = attempt
    sid = "pub-session-2"
    with SessionLocal() as db:
        db.add(BuildSession(session_id=sid, origin_compute_node_id=fence.owner_node_id))
        db.flush()
        store_layer_timings(sid, [_file(10_000)], db)
        db.commit()
    with SessionLocal() as db:
        result.previous_session_tokens[sid] = session_publication_token(db.get(BuildSession, sid))
    prepared = prepare_layer_timings([_file(40_000)], source=fence.as_dict())
    result.layer_timings[sid] = prepared
    result.sessions[sid] = {
        "files": [],
        "group": {"timing_publication_id": prepared.manifest["publication_id"]},
    }
    result.reports["pub-report-2"] = {
        "report_id": "pub-report-2",
        "session_id": sid,
        MANIFEST_KEY: prepared.manifest,
    }
    result.job.session_ids.append(sid)
    result.job.report_ids.append("pub-report-2")
    reports = prepare_import_reports(result)
    from analytics.prediction.layer_timings import replace_layer_timings

    def fail_second(session_id, prepared, db):
        if session_id == sid:
            raise RuntimeError("second session failed")
        return replace_layer_timings(session_id, prepared, db)

    monkeypatch.setattr("domain.services.importing.publication.replace_layer_timings", fail_second)
    with pytest.raises(RuntimeError, match="second session"), SessionLocal() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)
        db.commit()
    _assert_old()
    with SessionLocal() as db:
        assert stored_timings(sid, db) == {1: (10_000.0, 9000.0)}


def test_actual_worker_parses_and_publishes_one_complete_attempt(tmp_path, monkeypatch):
    from domain.services.import_jobs import detect_import_candidate
    from worker.tasks import process_due_import_jobs

    settings = get_settings()
    monkeypatch.setattr(settings, "compute_node_id", "pub-node")
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "29.05.2026_time.log").write_text(
        "OLD_STATS: 1|9000|30000|39300|\nOLD_STATS: 2|9000|31000|40300|\n",
        encoding="utf-8",
    )
    job = detect_import_candidate(folder, settings=settings).job
    job.status = ImportJobStatus.checking_stability
    job.session_ids = ["old-attempt"]
    job.report_ids = ["old-report"]
    with SessionLocal() as db:
        RuntimeRepository(db).save_import_job(job)
        db.commit()

    assert process_due_import_jobs("actual-pub-worker") == 1

    with SessionLocal() as db:
        saved = db.get(ImportJob, job.import_job_id)
        assert saved.status == "done"
        assert saved.lease_owner is None
        assert len(saved.session_ids) == 1 and "old-attempt" not in saved.session_ids
        assert len(saved.report_ids) == 1 and "old-report" not in saved.report_ids
        sid = saved.session_ids[0]
        assert stored_timings(sid, db) == {1: (30_000.0, 9000.0), 2: (31_000.0, 9000.0)}
        manifest = db.get(BuildSession, sid).context[MANIFEST_KEY]
        assert manifest["source"]["import_job_id"] == saved.import_job_id
        assert manifest["source"]["lease_generation"] == saved.lease_generation
        assert db.get(ReportArtifact, saved.report_ids[0]).payload[MANIFEST_KEY] == manifest
