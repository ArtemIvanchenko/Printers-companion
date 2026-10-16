"""First SAVEPOINT writes stay inside the caller's SQLite unit of work."""

import sqlite3
from contextlib import closing, contextmanager

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from domain.models.jobs import BackgroundJob
from domain.models.prints import MachineParams
from storage.db.session import SessionLocal, engine, get_db, session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository, lock_estimation_configuration


def _enqueue(db, suffix="outer"):
    return JobsRepository(db).enqueue(
        job_type="defect_model_retrain",
        owner_node_id="sqlite-savepoint-test",
        entity_type="model",
        entity_id=suffix,
        idempotency_key=f"sqlite-savepoint:{suffix}",
    )


def _job_ids():
    with SessionLocal() as db:
        return list(db.scalars(select(BackgroundJob.job_id)))


@contextmanager
def _trace(db):
    statements = []
    connection = db.connection()
    driver = connection.connection.driver_connection
    driver.set_trace_callback(statements.append)
    try:
        yield statements
    finally:
        if not connection.closed:
            driver.set_trace_callback(None)


@pytest.mark.parametrize("prior_read", [False, True])
def test_first_released_job_savepoint_is_rolled_back(prior_read):
    with SessionLocal() as db:
        if prior_read:
            assert db.scalar(select(MachineParams.id)) is None
        _enqueue(db)
        db.rollback()
    assert _job_ids() == []


def test_exception_in_session_scope_rolls_back_first_savepoint():
    with pytest.raises(RuntimeError, match="outer failure"):
        with session_scope() as db:
            _enqueue(db)
            raise RuntimeError("outer failure")
    assert _job_ids() == []


def test_exception_in_request_dependency_rolls_back_first_savepoint():
    dependency = get_db()
    db = next(dependency)
    _enqueue(db)
    with pytest.raises(RuntimeError, match="request failure"):
        dependency.throw(RuntimeError("request failure"))
    assert _job_ids() == []


def test_session_close_rolls_back_first_savepoint():
    with SessionLocal() as db:
        _enqueue(db)
    assert _job_ids() == []


def test_released_job_savepoint_is_only_visible_after_outer_commit():
    with SessionLocal() as db:
        job = _enqueue(db)
        assert _job_ids() == []
        db.commit()
    assert _job_ids() == [job["job_id"]]


def test_begin_precedes_first_savepoint_and_is_not_repeated_for_nested_writes():
    with SessionLocal() as db, _trace(db) as statements:
        db.execute(text("SELECT 1"))
        assert not db.connection().connection.driver_connection.in_transaction
        _enqueue(db)
        _enqueue(db, "second")
        db.rollback()
    begins = [index for index, sql in enumerate(statements) if sql.startswith("BEGIN")]
    savepoints = [index for index, sql in enumerate(statements) if sql.startswith("SAVEPOINT")]
    assert len(begins) == 1 and len(savepoints) == 2
    assert statements[begins[0]] == "BEGIN"
    assert begins[0] < savepoints[0]
    assert any(sql.startswith("ROLLBACK") for sql in statements)
    assert _job_ids() == []


def test_nested_rollback_keeps_outer_job_and_removes_inner_job():
    with SessionLocal() as db:
        outer = _enqueue(db)
        nested = db.begin_nested()
        _enqueue(db, "inner")
        nested.rollback()
        db.commit()
    assert _job_ids() == [outer["job_id"]]


def test_existing_configuration_reservation_is_not_restarted_by_savepoint():
    with SessionLocal() as db, _trace(db) as statements:
        lock_estimation_configuration(db)
        assert db.connection().connection.driver_connection.in_transaction
        _enqueue(db)
        lock_estimation_configuration(db)
        db.rollback()
    assert [sql for sql in statements if sql.startswith("BEGIN")] == ["BEGIN IMMEDIATE"]
    assert _job_ids() == []


def test_configuration_guard_reserves_writer_until_outer_rollback():
    with SessionLocal() as db, closing(sqlite3.connect(engine.url.database, timeout=.05)) as contender:
        db.execute(text("SELECT 1"))
        lock_estimation_configuration(db)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.execute("BEGIN IMMEDIATE")
        db.rollback()
        contender.execute("BEGIN IMMEDIATE")
        contender.rollback()


def test_first_savepoint_write_already_reserves_writer_for_later_guard():
    with SessionLocal() as db, closing(sqlite3.connect(engine.url.database, timeout=.05)) as contender:
        _enqueue(db)
        lock_estimation_configuration(db)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            contender.execute("BEGIN IMMEDIATE")
        db.rollback()
        contender.execute("BEGIN IMMEDIATE")
        contender.rollback()
    assert _job_ids() == []


def test_stale_read_does_not_block_writer_and_guard_refreshes_configuration():
    with session_scope() as db:
        PrintsRepository(db).save_machine_params({"time_correction_factor": 1.2})
    with SessionLocal() as stale_db:
        assert stale_db.get(MachineParams, 1).time_correction_factor == 1.2
        assert not stale_db.connection().connection.driver_connection.in_transaction
        with session_scope() as writer:
            PrintsRepository(writer).save_machine_params({"time_correction_factor": 1.4})
        current = PrintsRepository(stale_db).save_machine_params({"correction_locked": True})
        assert current["time_correction_factor"] == 1.4
        stale_db.rollback()


def test_failed_physical_begin_does_not_create_savepoint_or_publish_job():
    statements = []

    def reject_begin(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement == "BEGIN":
            raise RuntimeError("physical begin rejected")

    with SessionLocal() as db:
        event.listen(engine, "before_cursor_execute", reject_begin)
        try:
            with pytest.raises(RuntimeError, match="physical begin rejected"):
                _enqueue(db)
        finally:
            event.remove(engine, "before_cursor_execute", reject_begin)
            db.rollback()
        assert not any(sql.startswith(("SAVEPOINT", "INSERT")) for sql in statements)
        assert _job_ids() == []
        recovered = _enqueue(db, "recovered")
        db.commit()
    assert _job_ids() == [recovered["job_id"]]


@pytest.mark.parametrize("route, accepted", [
    ("/models/defect-risk/retrain", 200), ("/prints/recalibrate", 202),
])
@pytest.mark.parametrize("commit_fails", [False, True])
def test_http_queue_is_published_only_by_successful_outer_commit(monkeypatch, route, accepted, commit_fails):
    from fastapi.testclient import TestClient
    from api.main import app

    def reject_commit(self):
        raise RuntimeError("request commit rejected")

    with monkeypatch.context() as patch:
        if commit_fails:
            patch.setattr(Session, "commit", reject_commit)
        response = TestClient(app, raise_server_exceptions=False).post(route)
    if commit_fails:
        assert response.status_code == 500
        assert _job_ids() == []
    else:
        assert response.status_code == accepted
        assert _job_ids() == [response.json()["job_id"]]
