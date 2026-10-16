"""Physical-source regressions through the standard isolated test harness.

Boundary IO and controlled retry clocks are injected. The actual intake,
consumer, parsers, checkpoint and publication execute unchanged; fixture clock
changes do not measure physical elapsed time.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import zipfile
import json
import os

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from core.config.settings import get_settings
from domain.models.events import LayerSnapshot
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact, SourceFile
from domain.services.importing.fence import ImportFence
from domain.services.importing.requests import apply_import_action
from domain.services.importing.uploads import LogUpload, upload_log_batch
from storage.db.session import SessionLocal
from storage.repositories.prints_repo import PrintsRepository
from worker.tasks import process_due_import_jobs

MAIN = "01.04.2026.log"
TIMING = "01.04.2026_time.log"
CONTAINER = "late-logs.zip"
MAIN_BYTES = b"2026-04-01 10:00:00 start\n2026-04-01 10:00:01 finish\n"
TIMING_BYTES = b"OLD_STATS: 1|9000|30000|39300|\nOLD_STATS: 2|9000|31000|40300|\n"
MORE_TIMING = TIMING_BYTES + b"OLD_STATS: 3|10000|31000|41300|\n"


class SimulatedCrash(BaseException):
    pass


@pytest.fixture
def scenario(tmp_path, monkeypatch, request):
    settings = get_settings()
    assert settings.app_env == "test"
    monkeypatch.setattr(settings, "raw_logs_container_path", str(tmp_path / "raw"))
    monkeypatch.setattr(settings, "compute_node_id", "physical-checkpoint-review")
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    monkeypatch.setattr(settings, "file_stability_retry_seconds", 1)
    monkeypatch.setattr(settings, "file_stability_max_retries", 2)
    monkeypatch.setattr(settings, "nas_sync_retry_min_seconds", 1)
    monkeypatch.setattr(settings, "nas_sync_retry_max_seconds", 1)
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    monkeypatch.setattr(settings, "nas_outbox_path", str(tmp_path / "outbox"))
    monkeypatch.setenv("OPERATOR_INSTANCE_FILE", str(tmp_path / "identity"))
    Path(settings.raw_logs_container_path).mkdir(parents=True)
    uploads = [LogUpload(MAIN, BytesIO(MAIN_BYTES))]
    initial_kind = getattr(request, "param", None)
    retain_receipt = isinstance(initial_kind, str) and initial_kind.startswith("receipt-")
    if retain_receipt:
        initial_kind = initial_kind.removeprefix("receipt-")
    if initial_kind == "time":
        uploads.append(LogUpload(TIMING, BytesIO(TIMING_BYTES)))
    elif initial_kind == "zip":
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            entry = zipfile.ZipInfo("nested/" + TIMING, date_time=(2026, 4, 1, 10, 0, 0))
            archive.writestr(entry, TIMING_BYTES)
        uploads.append(LogUpload(CONTAINER, BytesIO(buffer.getvalue())))
    unlink = Path.unlink

    def retain_committed_receipt(path, *args, **kwargs):
        if path.parent.name == "incoming" and path.suffix == ".json":
            raise OSError("simulated acknowledged receipt cleanup failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as upload_patch:
        if retain_receipt:
            upload_patch.setattr(Path, "unlink", retain_committed_receipt)
        with SessionLocal() as db:
            response = upload_log_batch(PrintsRepository(db), uploads, settings=settings)
    job_id = response["jobs"][0]["import_job_id"]
    with SessionLocal() as db:
        row = db.get(ImportJob, job_id)
        source = Path(row.source_path)
        apply_import_action(db, job_id, "confirm", settings=settings, actor="checkpoint-operator")
        db.commit()


    class Store:
        settings = SimpleNamespace(minio_bucket_raw="raw-logs", minio_bucket_reports="reports")

        def __init__(self):
            self.objects = {}
            self.archives = []
            self.names = set()
            self.after_archive = None
            self.cut = False
            self.snapshot_at_first_archive = None
            self.snapshot_at_complete_archive = None

        def is_available(self):
            return True

        def put_file_verified(self, bucket, name, path, *, expected_sha256, expected_size):
            data = path.read_bytes()
            assert sha256(data).hexdigest() == expected_sha256
            assert len(data) == expected_size
            self.objects[name] = data
            if name.startswith("imports/"):
                if self.snapshot_at_first_archive is None:
                    self.snapshot_at_first_archive = set(load().file_snapshot)
                self.archives.append((path.name, expected_sha256, expected_size))
                self.names.add(path.name)
                if len(self.names) == 2 and not self.cut and self.after_archive is not None:
                    self.cut = True
                    self.snapshot_at_complete_archive = set(load().file_snapshot)
                    self.after_archive()
            return f"s3://{bucket}/{name}"

        def put_bytes_verified(self, bucket, name, data, **kwargs):
            self.objects[name] = data
            return f"s3://{bucket}/{name}"

    store = Store()
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", lambda: store)

    def load():
        with SessionLocal() as db:
            return db.get(ImportJob, job_id)

    def step():
        with SessionLocal() as db:
            row = db.get(ImportJob, job_id)
            if row.status == "postponed":
                row.postponed_until = datetime.now(timezone.utc) - timedelta(seconds=5)
            db.commit()
        process_due_import_jobs("physical-checkpoint-consumer")
        return load()

    def until_terminal(limit=6):
        for _ in range(limit):
            row = step()
            if row.status in {"done", "failed"}:
                return row
        pytest.fail("consumer did not reach a bounded terminal state")

    def until_cut(limit=6):
        for _ in range(limit):
            row = step()
            if store.cut:
                return row
            assert row.status not in {"done", "failed"}, "terminated before complete archive boundary"
        pytest.fail("did not reach complete archive boundary")

    def add(kind, data=TIMING_BYTES):
        target = source / (TIMING if kind == "time" else CONTAINER)
        if kind == "time":
            target.write_bytes(data)
        else:
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
                entry = zipfile.ZipInfo("nested/" + TIMING, date_time=(2026, 4, 1, 10, 0, 0))
                archive.writestr(entry, data)
        return target

    def insert_after_stability(kind, before_execute=None):
        import domain.services.import_jobs as importing
        execute = importing.execute_confirmed_import
        inserted = False

        def wrapper(*args, **kwargs):
            nonlocal inserted
            if not inserted:
                inserted = True
                if kind is not None:
                    add(kind)
                if before_execute is not None:
                    before_execute(args[0], kwargs)
            return execute(*args, **kwargs)

        monkeypatch.setattr(importing, "execute_confirmed_import", wrapper)

    def before_capture(kind, callback):
        import domain.services.import_jobs as importing
        checkpoint = importing.checkpoint_import_sources
        target_name = TIMING if kind == "time" else CONTAINER

        def wrapper(fence, snapshot):
            checkpoint(fence, snapshot)
            if target_name in snapshot and not store.cut:
                store.cut = True
                store.snapshot_at_checkpoint = set(load().file_snapshot)
                callback()

        monkeypatch.setattr(importing, "checkpoint_import_sources", wrapper)

    def no_publication():
        with SessionLocal() as db:
            for model in (BuildSession, ReportArtifact, SourceFile, LayerSnapshot):
                assert list(db.scalars(select(model))) == [], model

    def complete(expected_rows=2, timing_data=TIMING_BYTES):
        row = load()
        assert row.status == "done"
        assert row.owner_node_id == settings.compute_node_id
        assert row.confirmed_by == "checkpoint-operator"
        assert row.lease_owner is None
        with SessionLocal() as db:
            sources = list(db.scalars(select(SourceFile)))
            assert {source.file_name for source in sources} == {MAIN, TIMING}
            timing_source = next(source for source in sources if source.file_name == TIMING)
            assert timing_source.checksum == sha256(timing_data).hexdigest()
            assert timing_source.size_bytes == len(timing_data)
            assert len(list(db.scalars(select(LayerSnapshot)))) == expected_rows
            session = db.get(BuildSession, row.session_ids[0])
            assert session.context["timing_publication"]["status"] == "complete"
            report = db.get(ReportArtifact, row.report_ids[0])
            assert report.payload["timing_publication"] == session.context["timing_publication"]
        return row

    return SimpleNamespace(
        settings=settings, source=source, store=store, load=load, step=step,
        until_terminal=until_terminal, until_cut=until_cut, add=add, insert=insert_after_stability,
        before_capture=before_capture,
        no_publication=no_publication, complete=complete, job_id=job_id,
        preserved=tmp_path / "preserved-input",
    )


@pytest.mark.parametrize("kind", ["time", "zip"])
def test_accepted_input_missing_before_capture_cannot_publish_partial(scenario, kind):
    scenario.add(kind)
    target = scenario.source / (TIMING if kind == "time" else CONTAINER)
    original = None

    def cut():
        nonlocal original
        original = target.read_bytes()
        target.rename(scenario.preserved)

    scenario.before_capture(kind, cut)
    first = scenario.until_cut()
    assert scenario.store.cut
    assert first.status == "postponed"
    assert scenario.store.snapshot_at_checkpoint == {MAIN, target.name}
    assert set(first.file_snapshot) == {MAIN, target.name}
    scenario.no_publication()
    final = scenario.until_terminal()
    assert final.status == "failed"
    assert final.stability_check_attempts == scenario.settings.file_stability_max_retries
    assert final.execution_attempts == 0
    assert set(final.file_snapshot) == {MAIN, target.name}
    scenario.no_publication()
    assert scenario.preserved.read_bytes() == original


@pytest.mark.parametrize("kind", ["time", "zip"])
def test_accepted_input_can_be_restored_before_capture_retry(scenario, kind):
    scenario.add(kind)
    target = scenario.source / (TIMING if kind == "time" else CONTAINER)
    scenario.before_capture(kind, lambda: target.rename(scenario.preserved))
    assert scenario.until_cut().status == "postponed"
    assert set(scenario.load().file_snapshot) == {MAIN, target.name}
    scenario.preserved.rename(target)
    assert scenario.until_terminal().status == "done"
    row = scenario.complete()
    assert set(row.file_snapshot) == {MAIN, target.name}
    assert set(row.checksum_manifest) == {MAIN, target.name}


@pytest.mark.parametrize("kind", ["time", "zip"])
def test_retry_uses_current_sha_after_growth_or_container_replacement(scenario, kind):
    scenario.add(kind)
    target = scenario.source / (TIMING if kind == "time" else CONTAINER)
    scenario.before_capture(kind, lambda: target.rename(scenario.preserved))
    assert scenario.until_cut().status == "postponed"
    assert set(scenario.load().file_snapshot) == {MAIN, target.name}
    # This replacement is before retry, not during ZIP expansion. Its current
    # container/source SHA must be archived anew; old accepted bytes are retained.
    scenario.add(kind, MORE_TIMING)
    current = target.read_bytes()
    assert scenario.until_terminal().status == "done"
    row = scenario.complete(expected_rows=3, timing_data=MORE_TIMING)
    assert row.file_snapshot[target.name]["size"] == len(current)
    assert (target.name, sha256(current).hexdigest(), len(current)) in scenario.store.archives
    assert scenario.preserved.exists()


@pytest.mark.parametrize("kind", ["time", "zip"])
def test_missing_original_input_still_blocks_late_input(scenario, kind):
    (scenario.source / MAIN).rename(scenario.preserved)
    scenario.add(kind)
    row = scenario.until_terminal()
    assert row.status == "failed"
    assert MAIN in row.file_snapshot
    assert scenario.store.archives == []
    scenario.no_publication()
    assert scenario.preserved.read_bytes() == MAIN_BYTES


@pytest.mark.parametrize("kind", ["time", "zip"])
def test_checkpoint_survives_uncaught_crash_before_archive(scenario, monkeypatch, kind):
    scenario.add(kind)
    target = scenario.source / (TIMING if kind == "time" else CONTAINER)

    from domain.services.import_jobs import archive_raw_import

    def crash(*args, **kwargs):
        if target.name in args[0].file_snapshot:
            raise SimulatedCrash("lost process before raw archive of accepted late input")
        return archive_raw_import(*args, **kwargs)

    monkeypatch.setattr("domain.services.import_jobs.archive_raw_import", crash)
    with pytest.raises(SimulatedCrash):
        for _ in range(6):
            scenario.step()
    row = scenario.load()
    assert set(row.file_snapshot) == {MAIN, target.name}
    assert row.status in {"checking_stability", "postponed"}
    assert row.lease_owner is not None
    crashed_generation = row.lease_generation
    assert all(name != target.name for name, _, _ in scenario.store.archives)
    target.rename(scenario.preserved)
    with SessionLocal() as db:
        row = db.get(ImportJob, scenario.job_id)
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.commit()
    row = scenario.step()
    assert row.status == "postponed"
    assert row.lease_generation == crashed_generation + 1
    assert set(row.file_snapshot) == {MAIN, target.name}
    scenario.no_publication()


@pytest.mark.parametrize("change", ["generation", "expiry", "owner"])
def test_checkpoint_rejects_lost_lease_even_when_preparation_guard_is_cached(scenario, change, monkeypatch):
    from core.maintenance import maintenance_active
    pause_requested = False
    monkeypatch.setattr("core.maintenance.maintenance_active", lambda: pause_requested or maintenance_active())

    def invalidate(claimed, kwargs):
        nonlocal pause_requested
        assert kwargs["lease_guard"]()
        with SessionLocal() as db:
            row = db.get(ImportJob, scenario.job_id)
            if change == "generation":
                row.lease_generation += 1
                row.lease_owner = "newer-consumer"
            elif change == "owner":
                row.owner_node_id = "other-operator"
            else:
                row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=5)
            db.commit()
        # The real consumer drains a queue and can reclaim an immediately expired
        # lease in this same invocation. Stop only its NEXT claim to inspect the
        # obsolete attempt; current preparation/checkpoint/fencing stay real.
        pause_requested = True

    scenario.insert(None, invalidate)
    scenario.step()
    row = scenario.load()
    assert set(row.file_snapshot) == {MAIN}
    assert scenario.store.archives == []
    assert row.status == "checking_stability"
    if change == "generation":
        assert row.lease_generation == 2
        assert row.lease_owner == "newer-consumer"
    elif change == "owner":
        assert row.owner_node_id == "other-operator"
    scenario.no_publication()


def test_checkpoint_commit_error_never_starts_raw_archive(scenario, monkeypatch):
    executing = False

    def in_execute(claimed, kwargs):
        nonlocal executing
        executing = True

    scenario.insert(None, in_execute)

    def fail_checkpoint(db):
        for item in db.identity_map.values():
            if executing and isinstance(item, ImportJob) and item.status == "checking_stability":
                raise RuntimeError("checkpoint commit rejected")

    event.listen(Session, "before_commit", fail_checkpoint)
    try:
        row = scenario.step()
    finally:
        event.remove(Session, "before_commit", fail_checkpoint)
    assert row.status == "postponed"
    assert set(row.file_snapshot) == {MAIN}
    assert scenario.store.archives == []
    scenario.no_publication()


def test_checkpoint_lost_ack_does_not_erase_committed_input_evidence(scenario):
    # First admit the new source through the normal single stability defer.
    # Then lose ACK only inside execute, not during claim/registration commits.
    scenario.add("time")
    assert scenario.step().status == "postponed"
    executing = False

    def in_execute(claimed, kwargs):
        nonlocal executing
        executing = True

    scenario.insert(None, in_execute)
    raised = False

    def fail_ack(db):
        nonlocal raised
        if executing and not raised and any(
            isinstance(item, ImportJob) and item.status in {"checking_stability", "postponed"}
            and item.lease_owner is not None and TIMING in item.file_snapshot
            for item in db.identity_map.values()
        ):
            raised = True
            raise RuntimeError("lost checkpoint acknowledgement after commit")

    event.listen(Session, "after_commit", fail_ack)
    try:
        row = scenario.step()
    finally:
        event.remove(Session, "after_commit", fail_ack)
    assert raised
    assert row.status == "postponed"
    assert set(row.file_snapshot) == {MAIN, TIMING}
    assert scenario.store.archives == []
    (scenario.source / TIMING).rename(scenario.preserved)
    assert scenario.until_terminal().status == "failed"
    scenario.no_publication()
    assert scenario.preserved.read_bytes() == TIMING_BYTES


def test_checkpoint_rechecks_late_expiry_before_commit(scenario, monkeypatch):
    scenario.insert(None)
    real_verify = ImportFence.verify
    calls = 0

    def expire_after_first_check(fence, db):
        nonlocal calls
        row = real_verify(fence, db)
        calls += 1
        if calls == 1:
            row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=5)
        return row

    monkeypatch.setattr(ImportFence, "verify", expire_after_first_check)
    scenario.step()
    row = scenario.load()
    assert set(row.file_snapshot) == {MAIN}
    assert scenario.store.archives == []
    assert calls >= 1
    scenario.no_publication()


def test_direct_coordinator_requires_a_claim_even_in_test_environment(scenario):
    from domain.services.import_jobs import confirm_import_job
    from domain.services.importing.fence import StaleImportLeaseError
    from profiles.m350.profile import build_registry, get_profile
    from storage.repositories.import_jobs import ImportJobsRepository

    with SessionLocal() as db:
        job = ImportJobsRepository(db).get_import_job(scenario.job_id)
    assert job.lease_owner is None
    with pytest.raises(StaleImportLeaseError):
        confirm_import_job(job, build_registry(), get_profile(), settings=scenario.settings)
    assert scenario.store.archives == []
    scenario.no_publication()


@pytest.mark.parametrize("kind", ["time", "zip"])
def test_arrival_after_stability_is_not_part_of_accepted_byte_generation(scenario, kind):
    scenario.insert(kind)
    assert scenario.step().status == "done"
    row = scenario.load()
    assert set(row.file_snapshot) == set(row.checksum_manifest) == {MAIN}
    assert {name for name, _, _ in scenario.store.archives} == {MAIN}
    with SessionLocal() as db:
        sources = list(db.scalars(select(SourceFile)))
        assert {source.file_name for source in sources} == {MAIN}
        assert list(db.scalars(select(LayerSnapshot))) == []
        session = db.get(BuildSession, row.session_ids[0])
        assert session.context["timing_publication"]["status"] == "no_time_log"
    arrived = scenario.source / (TIMING if kind == "time" else CONTAINER)
    assert arrived.is_file(), "unaccepted original must remain for a later explicit batch"


@pytest.mark.parametrize(
    "scenario,context,replace_original",
    [
        ("zip", "zip", False), ("zip", "zip", True),
        ("zip", "loose-in-zip", False), ("zip", "loose-in-zip", True),
        ("time", "direct-log", False), ("time", "direct-log", True),
    ],
    indirect=["scenario"],
)
def test_publication_is_one_verified_raw_byte_generation(scenario, context, replace_original):
    physical_name = CONTAINER if context == "zip" else MAIN if context == "loose-in-zip" else TIMING
    logical_name = TIMING if context != "loose-in-zip" else MAIN
    target = scenario.source / physical_name
    original_mtime_ns = 1_771_234_567_123_456_789
    os.utime(target, ns=(original_mtime_ns, original_mtime_ns))
    # Refresh the initial intake through the existing single stability policy,
    # not by rewriting any job source fields directly.
    assert scenario.step().status == "postponed"
    before = {}

    def at_full_archive():
        before["physical"] = target.read_bytes()
        before["mtime_ns"] = target.stat().st_mtime_ns
        if context == "zip":
            with zipfile.ZipFile(BytesIO(before["physical"])) as archive:
                assert archive.testzip() is None
                before["logical"] = archive.read("nested/" + TIMING)
        else:
            before["logical"] = before["physical"]
        if replace_original:
            if context == "zip":
                scenario.add("zip", MORE_TIMING)
            elif context == "direct-log":
                target.write_bytes(MORE_TIMING)
            else:
                target.write_bytes(before["physical"] + b"2026-04-01 10:00:02 operator marker\n")

    scenario.store.after_archive = at_full_archive
    result = scenario.until_cut()
    assert scenario.store.cut and before, "full verified archive callback did not run"
    old_physical = before["physical"]
    old_logical = before["logical"]
    key = next(
        key for key, data in scenario.store.objects.items()
        if key.startswith("imports/") and key.endswith("/" + physical_name) and data == old_physical
    )
    assert sha256(old_physical).hexdigest() in key
    if replace_original:
        assert target.read_bytes() != old_physical
    else:
        assert target.read_bytes() == old_physical
    with SessionLocal() as db:
        source = db.scalar(select(SourceFile).where(SourceFile.file_name == logical_name))
        layers = list(db.scalars(select(LayerSnapshot)))
        session = db.get(BuildSession, source.session_id) if source else None
        projection = next(
            (item for item in session.context["runtime_payload"]["files"] if item["relative_path"] == logical_name),
            None,
        ) if session else None
        evidence = {
            "context": context, "replace_original": replace_original, "status": result.status,
            "raw_bytes": len(old_physical), "raw_sha": sha256(old_physical).hexdigest(),
            "raw_member_bytes": len(old_logical), "raw_member_sha": sha256(old_logical).hexdigest(),
            "original_now_bytes": target.stat().st_size,
            "original_now_sha": sha256(target.read_bytes()).hexdigest(),
            "published_sha": source.checksum if source else None,
            "published_bytes": source.size_bytes if source else None,
            "published_uri": source.object_uri if source else None,
            "layers": len(layers), "captured_mtime_ns": before["mtime_ns"],
            "projection_mtime": projection["mtime"] if projection else None,
        }
        print(json.dumps(evidence, indent=2))
        assert result.status == "done", "captured and archived original generation must remain publishable"
        assert source is not None
        assert source.checksum == sha256(old_logical).hexdigest(), "published bytes are absent from referenced raw object"
        assert source.size_bytes == len(old_logical)
        assert source.object_uri.endswith(key)
        assert source.metadata_json["raw_file_name"] == logical_name
        assert len(layers) == 2, "changed ZIP introduced a third row outside the archived generation"
        assert projection["checksum"] == source.checksum
        assert projection["size_bytes"] == source.size_bytes
        if context == "zip":
            assert source.metadata_json["archive_member_path"] == "nested/" + TIMING
        if context == "direct-log":
            expected = datetime.fromtimestamp(before["mtime_ns"] / 1e9, timezone.utc)
            actual = datetime.fromisoformat(projection["mtime"].replace("Z", "+00:00"))
            assert actual == expected
            original = str(scenario.source / logical_name)
            assert source.original_path == original
            assert projection["path"] == original
            assert projection["classification"]["path"] == original


@pytest.mark.parametrize("scenario", ["receipt-time", "receipt-zip"], indirect=True)
def test_completed_import_reconciles_its_original_physical_upload_receipt(scenario, monkeypatch, tmp_path):
    import shutil
    from domain.services.importing import recovery
    from domain.services.importing.requests import register_import_candidates
    from storage.repositories.import_jobs import ImportJobsRepository

    receipt_path = scenario.source.with_name(scenario.source.name + ".json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected = {item["stored_name"]: item["checksum"] for item in receipt["files"]}
    assert scenario.until_terminal().status == "done"
    scenario.complete()
    with SessionLocal() as db:
        before = ImportJobsRepository(db).get_import_job(receipt["import_job_id"]).model_dump(mode="json")
    monkeypatch.setattr(recovery, "_verify_batch", lambda *args: pytest.fail("registered bytes must not be rehashed"))
    result = recovery.recover_upload_receipt(receipt_path, settings=get_settings())
    assert result == {"state": "registered", "job_id": receipt["import_job_id"]}
    with SessionLocal() as db:
        after = ImportJobsRepository(db).get_import_job(receipt["import_job_id"]).model_dump(mode="json")
    assert before == after, "receipt reconciliation must not reset completion, lease or analytical facts"
    assert after["checksum_manifest"] == expected
    assert receipt_path.exists() and scenario.source.is_dir()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["recovery"]["state"] == "registered"
    assert recovery.recover_upload_receipt(receipt_path, settings=get_settings())["state"] == "skipped"
    # The same physical manifest also powers the existing copy-deduplication
    # contract; completed ZIP imports must not turn their identity into members.
    copied = tmp_path / "verified-copy" / scenario.source.name
    copied.mkdir(parents=True)
    for source in scenario.source.iterdir():
        shutil.copy2(source, copied / source.name)
    detected = register_import_candidates([copied], settings=get_settings())[0]
    assert detected.job.import_job_id == receipt["import_job_id"]
    assert detected.job.checksum_manifest == expected
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ImportJob)))) == 1
        assert len(list(db.scalars(select(LayerSnapshot)))) == 2
