from datetime import datetime, timedelta, timezone

import pytest
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.errors import (
    AdminShutdown, ConfigurationLimitExceeded, ConnectionFailure, ConnectionTimeout,
    DiskFull, InsufficientResources, InvalidPassword, OutOfMemory,
    StatementTooComplex, TooManyConnections, UndefinedColumn, UniqueViolation,
)
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, TimeoutError

from core.config.settings import Settings, get_settings
from domain.enums.common import ImportJobStatus
from domain.models.sessions import ImportJob, ReportArtifact
from domain.services.import_jobs import (
    ImportJobRecord,
    ImportPersistenceError,
    RawArchiveUnavailableError,
    check_source_stability,
    confirm_import_job,
    detect_import_candidate,
    queue_import_job_retry,
)
from domain.services.importing.fence import ImportFence, StaleImportLeaseError
from storage.db.session import SessionLocal, is_database_unavailable
from storage.repositories.import_jobs import ImportJobsRepository
from worker.tasks import _apply_import_failure_policy, process_due_import_jobs


def _job() -> ImportJobRecord:
    return ImportJobRecord(
        import_job_id="import_retry",
        owner_node_id="operator-01",
        source_path="/local/logs",
        source_name="logs",
        lease_owner="operator-01:worker",
        lease_generation=8,
        stability_check_attempts=9,
        execution_attempts=9,
    )


def test_nas_persistence_failure_does_not_spend_import_retry_budget():
    job = _job()
    now = datetime.now(timezone.utc)
    settings = Settings(
        nas_sync_retry_min_seconds=5,
        nas_sync_retry_max_seconds=300,
        file_stability_max_retries=10,
    )

    _apply_import_failure_policy(
        job,
        ImportPersistenceError("NAS database unavailable"),
        settings=settings,
        now=now,
    )

    assert job.status == ImportJobStatus.postponed
    assert job.stability_check_attempts == 9
    assert job.execution_attempts == 9
    assert job.postponed_until is not None
    assert (job.postponed_until - now).total_seconds() == 300
    assert job.lease_owner is None


def test_unexpected_import_failure_remains_bounded_until_explicit_retry():
    job = _job()
    now = datetime.now(timezone.utc)
    settings = Settings(file_stability_max_retries=10)

    _apply_import_failure_policy(
        job,
        RuntimeError("invalid parser state"),
        settings=settings,
        now=now,
    )

    assert job.status == ImportJobStatus.failed
    assert job.stability_check_attempts == 9
    assert job.execution_attempts == 10
    assert job.postponed_until is None

    queue_import_job_retry(job, now=now)
    assert job.status == ImportJobStatus.checking_stability
    assert job.execution_attempts == 0
    assert job.stability_check_attempts == 0


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("error,outage", [
    pytest.param(OperationalError("SQL", None, RuntimeError("disconnect"), connection_invalidated=True), True, id="invalidated"),
    pytest.param(OperationalError("SQL", None, ConnectionFailure("disconnect")), True, id="connection_sqlstate"),
    pytest.param(OperationalError("SQL", None, AdminShutdown("shutdown")), True, id="server_shutdown"),
    pytest.param(OperationalError(None, None, TooManyConnections("capacity", pgconn=object())), True, id="connection_capacity"),
    pytest.param(OperationalError(None, None, ConnectionTimeout("connect timeout")), True, id="connect_timeout"),
    pytest.param(OperationalError(None, None, PsycopgOperationalError("connect failed", pgconn=object())), True, id="connect_attempt"),
    pytest.param(IntegrityError("SQL", None, UniqueViolation("constraint"), connection_invalidated=True), False, id="constraint"),
    pytest.param(ProgrammingError("SQL", None, UndefinedColumn("syntax")), False, id="programming"),
    pytest.param(OperationalError("SQL", None, RuntimeError("unknown failure")), False, id="unknown_operational"),
    pytest.param(OperationalError("SQL", None, InvalidPassword("bad configuration")), False, id="authentication"),
    pytest.param(OperationalError("SQL", None, StatementTooComplex("SQL resource limit")), False, id="query_limit"),
    pytest.param(OperationalError("SQL", None, InsufficientResources("unknown resource limit")), False, id="unknown_resource_limit"),
    pytest.param(OperationalError("SQL", None, DiskFull("disk full")), False, id="disk_full"),
    pytest.param(OperationalError("SQL", None, OutOfMemory("query memory allocation")), False, id="out_of_memory"),
    pytest.param(OperationalError("SQL", None, ConfigurationLimitExceeded("configuration limit")), False, id="configuration_limit"),
    pytest.param(TimeoutError("connection pool exhausted"), False, id="pool_exhaustion"),
])
def test_sql_failure_policy_uses_connectivity_evidence_and_preserves_bounded_bugs(error, outage, wrapped):
    assert is_database_unavailable(error) is outage
    if wrapped:
        wrapper = ImportPersistenceError("could not persist source")
        wrapper.__cause__ = error
        error = wrapper
    job = _job()
    now = datetime.now(timezone.utc)
    settings = Settings(file_stability_max_retries=10)
    _apply_import_failure_policy(job, error, settings=settings, now=now)
    assert job.status == (ImportJobStatus.postponed if outage else ImportJobStatus.failed)
    assert job.execution_attempts == (9 if outage else 10)
    assert job.stability_check_attempts == 9


@pytest.mark.parametrize("wrapped", [False, True])
def test_connection_capacity_outage_does_not_exhaust_execution_budget(wrapped):
    error = OperationalError(None, None, TooManyConnections("all connection slots are in use"))
    if wrapped:
        wrapper = ImportPersistenceError("could not persist source")
        wrapper.__cause__ = error
        error = wrapper
    job = _job()
    job.execution_attempts = 0
    settings = Settings(file_stability_max_retries=3, nas_sync_retry_min_seconds=1, nas_sync_retry_max_seconds=4)
    now = datetime.now(timezone.utc)
    delays = []
    for generation in range(1, 4):
        job.lease_generation = generation
        _apply_import_failure_policy(job, error, settings=settings, now=now)
        delays.append((job.postponed_until - now).total_seconds() if job.postponed_until else None)
        now = job.postponed_until or now
    assert job.status == ImportJobStatus.postponed
    assert job.execution_attempts == 0
    assert job.stability_check_attempts == 9
    assert delays == [1, 2, 4]


def test_nas_file_change_and_processing_error_have_independent_retry_budgets(
    tmp_path, monkeypatch
):
    source = tmp_path / "logs"
    source.mkdir()
    log = source / "job.log"
    log.write_text("original", encoding="utf-8")
    now = datetime.now(timezone.utc)
    settings = Settings(file_stability_seconds=0, file_stability_max_retries=2)
    job = detect_import_candidate(source, settings=settings, now=now).job
    job.lease_generation = 8

    def unavailable(*args, **kwargs):
        raise RawArchiveUnavailableError("NAS unavailable")

    monkeypatch.setattr("domain.services.import_jobs.execute_confirmed_import", unavailable)
    confirm_import_job(job, registry=object(), settings=settings, now=now)
    assert job.status == ImportJobStatus.postponed
    assert job.stability_check_attempts == 0
    assert job.execution_attempts == 0

    log.write_text("changed after NAS outage", encoding="utf-8")
    now += timedelta(seconds=5)
    confirm_import_job(job, registry=object(), settings=settings, now=now)
    assert job.status == ImportJobStatus.postponed
    assert job.stability_check_attempts == 1
    assert job.execution_attempts == 0
    assert check_source_stability(job, source, settings=settings, now=now).stable

    _apply_import_failure_policy(job, RuntimeError("report preparation failed"), settings=settings, now=now)
    assert job.status == ImportJobStatus.postponed
    assert job.stability_check_attempts == 1
    assert job.execution_attempts == 1


def test_execution_budget_survives_roundtrip_claim_and_outage_until_explicit_retry():
    settings = Settings(file_stability_max_retries=10, file_stability_retry_seconds=30)
    now = datetime.now(timezone.utc)
    job = _job()
    job.status = ImportJobStatus.checking_stability
    job.confirmed_by = "operator"
    job.lease_owner = None
    job.lease_until = None
    job.stability_check_attempts = 4
    job.execution_attempts = 2
    with SessionLocal() as db:
        ImportJobsRepository(db).save_import_job(job)
        db.commit()

    with SessionLocal() as db:
        repo = ImportJobsRepository(db)
        first = repo.claim_next_import_job(owner_node_id=job.owner_node_id, lease_owner="first", now=now)
        db.commit()
        assert first.execution_attempts == 2
        old_fence = ImportFence(first.import_job_id, first.owner_node_id, first.lease_owner, first.lease_generation)
    with SessionLocal() as db:
        old_fence.verify(db)
        _apply_import_failure_policy(first, RuntimeError("processing failed"), settings=settings, now=now)
        ImportJobsRepository(db).save_import_job(first)
        db.commit()

    with SessionLocal() as db:
        repo = ImportJobsRepository(db)
        assert repo.claim_next_import_job(owner_node_id="another-node", lease_owner="foreign", now=first.postponed_until) is None
        second = repo.claim_next_import_job(owner_node_id=job.owner_node_id, lease_owner="second", now=first.postponed_until)
        db.commit()
        assert second.execution_attempts == 3
        assert second.stability_check_attempts == 4
        assert second.lease_generation == first.lease_generation + 1
    with SessionLocal() as db, pytest.raises(StaleImportLeaseError):
        old_fence.verify(db)
    with SessionLocal() as db:
        _apply_import_failure_policy(second, ImportPersistenceError("NAS unavailable"), settings=settings, now=now)
        ImportJobsRepository(db).save_import_job(second)
        db.commit()
    with SessionLocal() as db:
        restored = ImportJobsRepository(db).get_import_job(job.import_job_id)
        assert restored.execution_attempts == 3
        assert restored.stability_check_attempts == 4
        queue_import_job_retry(restored, now=now)
        assert restored.execution_attempts == 0
        assert restored.stability_check_attempts == 0
        ImportJobsRepository(db).save_import_job(restored)
        db.commit()
    with SessionLocal() as db:
        reclaimed = ImportJobsRepository(db).claim_next_import_job(
            owner_node_id=job.owner_node_id, lease_owner="third", now=now
        )
        assert reclaimed.execution_attempts == 0
        assert reclaimed.lease_generation == second.lease_generation + 1


@pytest.mark.parametrize("failure", [
    "processing", "replaced_lease", "connection_fence", "connection_commit", "connection_capacity",
    "integrity_staging", "programming_staging", "integrity_commit", "programming_commit",
])
def test_execution_budget_resets_only_with_successfully_committed_publication(
    tmp_path, monkeypatch, failure
):
    import worker.tasks as tasks

    settings = get_settings()
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    monkeypatch.setattr(settings, "file_stability_max_retries", 10)
    monkeypatch.setattr("core.maintenance.maintenance_active", lambda: False)
    source = tmp_path / "logs"
    source.mkdir()
    (source / "job.log").write_text(
        "2026-04-27 10:00:00 Старт печати\n2026-04-27 10:01:00 слой 1\n", encoding="utf-8"
    )
    job = detect_import_candidate(source, settings=settings).job
    job.status = ImportJobStatus.checking_stability
    job.confirmed_by = "operator"
    job.stability_check_attempts = 9
    job.execution_attempts = 2
    with SessionLocal() as db:
        ImportJobsRepository(db).save_import_job(job)
        db.commit()

    original_publish = tasks.publish_import

    if failure == "connection_capacity":
        sql_error = OperationalError(None, None, TooManyConnections("connection capacity exhausted"))
    elif failure.startswith("connection"):
        sql_error = OperationalError(
            "publication SQL", None, ConnectionFailure("simulated disconnect"),
            connection_invalidated=failure == "connection_commit",
        )
    elif failure.startswith("integrity"):
        sql_error = IntegrityError("publication SQL", None, UniqueViolation("simulated constraint violation"))
    else:
        sql_error = ProgrammingError("publication SQL", None, UndefinedColumn("simulated SQL bug"))

    def fail_publication(db, *args, **kwargs):
        if failure == "replaced_lease":
            row = db.get(ImportJob, job.import_job_id)
            row.lease_generation += 1
            row.lease_owner = "replacement-worker"
            db.commit()
            return original_publish(db, *args, **kwargs)
        if failure in {"connection_fence", "connection_capacity"}:
            def unavailable(*args):
                raise sql_error
            with monkeypatch.context() as patch:
                patch.setattr(ImportFence, "verify", unavailable)
                return original_publish(db, *args, **kwargs)
        original_publish(db, *args, **kwargs)
        if failure.endswith("commit"):
            def fail_commit():
                raise sql_error
            monkeypatch.setattr(db, "commit", fail_commit)
            return
        raise RuntimeError("simulated failure before publication commit")

    with monkeypatch.context() as patch:
        if failure.endswith("staging"):
            def fail_source(*args, **kwargs):
                raise sql_error
            patch.setattr("storage.repositories.runtime.RuntimeRepository.save_source_file", fail_source)
        else:
            patch.setattr(tasks, "publish_import", fail_publication)
        assert process_due_import_jobs(lease_owner="publication-test-worker") == 0
    with SessionLocal() as db:
        row = db.get(ImportJob, job.import_job_id)
        assert row.status == ("checking_stability" if failure == "replaced_lease" else "postponed")
        outage = failure.startswith("connection")
        assert row.execution_attempts == (2 if failure == "replaced_lease" or outage else 3)
        assert row.stability_check_attempts == 9
        assert db.query(ReportArtifact).count() == 0
        if failure == "replaced_lease":
            assert row.lease_owner == "replacement-worker"
            row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            assert row.lease_owner is None
            expected_delay = settings.nas_sync_retry_min_seconds if outage else settings.file_stability_retry_seconds
            assert (row.postponed_until - row.updated_at).total_seconds() == expected_delay
            row.postponed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    assert process_due_import_jobs(lease_owner="publication-test-worker") == 1
    with SessionLocal() as db:
        row = db.get(ImportJob, job.import_job_id)
        assert row.status == "done"
        assert row.execution_attempts == 0
        assert row.stability_check_attempts == 9
        assert row.lease_generation == (3 if failure == "replaced_lease" else 2)
        assert db.query(ReportArtifact).count() == 1
