"""Estimate identity/locking must not hydrate the session's telemetry JSON."""

from datetime import datetime, timezone
import pytest
from sqlalchemy import event, select

from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from domain.models.sessions import BuildSession, Printer, PrinterProfile
from domain.services.estimation.inputs import prepare_prediction_inputs, prediction_input_hash
from domain.services.estimation.publication import publish_estimate
from storage.db.session import engine, session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository


@pytest.fixture
def linked_estimate():
    owner = get_settings().compute_node_id
    context = {"telemetry": [{"ts": index, "SO1": .01, "ST3": 125.0,
                              "raw": "original source evidence " * 10} for index in range(12000)]}
    with session_scope() as db:
        db.add(PrinterProfile(profile_id="projection-profile", vendor="fixture", model_family="SLM",
                              current_version="fixture"))
        db.flush()
        db.add(Printer(printer_id="projection-machine", name="fixture", vendor="fixture",
                       model_family="SLM", profile_id="projection-profile"))
        db.flush()
        db.add(BuildSession(session_id="projection-session", printer_id="projection-machine",
                            origin_compute_node_id=owner, context=context))
        db.flush()
        repo = PrintsRepository(db)
        repo.save_machine_params({"hatch_speed_mm_s": 1000.0, "contour_speed_mm_s": 500.0,
            "hatch_distance_mm": .1, "layer_thickness_mm": .06, "laser_count": 1})
        record = repo.create_print_record({"name": "projection fixture", "material": "steel",
            "session_id": "projection-session", "metadata_json": {"printer_id": "metadata-machine"}})
        repo.add_print_file({"record_id": record["record_id"], "file_name": "fixture.stl",
            "file_type": "stl", "checksum": "a" * 64, "size_bytes": 1,
            "object_uri": "s3://isolated/fixture.stl"})
    return record["record_id"], context


def _session_selects(statements):
    return [sql for sql in statements if "from sessions" in sql.lower()]


def test_preparation_keeps_inputs_without_reading_telemetry(linked_estimate):
    record_id, context = linked_estimate
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with session_scope() as db:
            prepared = prepare_prediction_inputs(PrintsRepository(db), record_id)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert prepared["printer_id"] == prepared["params"]["printer_id"] == "projection-machine"
    assert prepared["material"] == "steel"
    assert prepared["record"]["session_id"] == "projection-session"
    assert prepared["platform_files"][0]["checksum"] == "a" * 64
    assert _session_selects(statements)
    assert all("sessions.context" not in sql for sql in _session_selects(statements))
    with session_scope() as db:
        assert db.scalar(select(BuildSession.context)) == context


def test_unknown_linked_machine_still_uses_card_metadata(linked_estimate):
    record_id, _ = linked_estimate
    with session_scope() as db:
        db.get(BuildSession, "projection-session").printer_id = None
        db.flush()
        prepared = prepare_prediction_inputs(PrintsRepository(db), record_id)
    assert prepared["printer_id"] == prepared["params"]["printer_id"] == "metadata-machine"


def test_fenced_publication_locks_linked_row_without_reading_telemetry(linked_estimate):
    record_id, context = linked_estimate
    owner = get_settings().compute_node_id
    with session_scope() as db:
        prepared = prepare_prediction_inputs(PrintsRepository(db), record_id)
        job = JobsRepository(db).enqueue(job_type="print_estimate", owner_node_id=owner,
            entity_type="print_record", entity_id=record_id, idempotency_key="projection-estimate",
            payload={"record_id": record_id, "owner_node_id": owner,
                     "record_revision": prepared["record"]["revision"]})
        claim = JobsRepository(db).claim_next("print_estimate", owner_node_id=owner,
                                              lease_owner="projection-consumer")
    snapshot = {"input_hash": prediction_input_hash(prepared),
        "input_revision": prepared["record"]["revision"], "print_hours": 1.0,
        "correction_factor": 1.0, "estimated_at": datetime.now(timezone.utc).isoformat()}
    statements = []

    def observe(conn, cursor, statement, parameters, execution_context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with session_scope() as db:
            publish_estimate(db, prepared, snapshot, job_id=job["job_id"], owner_node_id=owner,
                lease_owner="projection-consumer", lease_generation=claim["lease_generation"])
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert _session_selects(statements)
    assert all("sessions.context" not in sql for sql in _session_selects(statements))
    with session_scope() as db:
        assert db.get(BackgroundJob, job["job_id"]).status == "done"
        assert PrintsRepository(db).get_print_record(record_id)["metadata_json"]["prediction"] == snapshot
        assert db.scalar(select(BuildSession.context)) == context
