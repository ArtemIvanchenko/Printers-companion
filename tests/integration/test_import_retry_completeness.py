"""Native cross-retry import completeness and shared source policy."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import time
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy import select

from core.config.settings import get_settings
from domain.models.events import LayerSnapshot
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact, SourceFile
from domain.services.importing.requests import apply_import_action, register_import_candidates
from domain.services.importing.uploads import LogUpload, upload_log_batch
from storage.db.session import SessionLocal
from storage.repositories.prints_repo import PrintsRepository
from worker.tasks import process_due_import_jobs
from profiles.m350.profile import get_profile

MAIN = "01.04.2026.log"
TIMING = "01.04.2026_time.log"
CONTAINER = "accepted-logs.zip"
MAIN_BYTES = "2026-04-01 10:00:00 Старт печати\n2026-04-01 10:00:01 Прожиг слоя 1\n".encode()
TIMING_BYTES = b"OLD_STATS: 1|9000|30000|39300|\nOLD_STATS: 2|9000|31000|40300|\n"


class VerifiedStore:
    def __init__(self):
        self.settings = SimpleNamespace(minio_bucket_raw="raw-logs", minio_bucket_reports="reports")
        self.objects = {}
        self.archive_names = set()
        self.expected_names = {MAIN, TIMING}
        self.after_archive = None
        self.called = False

    def is_available(self):
        return True

    def put_file_verified(self, bucket, object_name, path, *, expected_sha256, expected_size):
        data = path.read_bytes()
        assert sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size
        self.objects[object_name] = data
        if object_name.startswith("imports/"):
            self.archive_names.add(path.name)
            if self.expected_names <= self.archive_names and not self.called and self.after_archive:
                self.called = True
                self.after_archive()
        return f"s3://{bucket}/{object_name}"

    def put_bytes_verified(self, bucket, object_name, data, **kwargs):
        self.objects[object_name] = data
        return f"s3://{bucket}/{object_name}"


@pytest.fixture
def batch(tmp_path, monkeypatch):
    settings = get_settings()
    assert settings.app_env == "test"
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(raw))
    monkeypatch.setattr(settings, "compute_node_id", "native-retry-policy-audit")
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    monkeypatch.setattr(settings, "file_stability_retry_seconds", 1)
    monkeypatch.setattr(settings, "file_stability_max_retries", 2)
    monkeypatch.setattr(settings, "nas_sync_retry_min_seconds", 1)
    monkeypatch.setattr(settings, "nas_sync_retry_max_seconds", 1)
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    monkeypatch.setenv("OPERATOR_INSTANCE_FILE", str(tmp_path / "operator" / "instance-id"))
    monkeypatch.setattr(settings, "nas_outbox_path", str(tmp_path / "outbox"))
    store = VerifiedStore()
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", lambda: store)
    # Use the actual selected profile; shrink only its existing size gate to
    # make its declared exclusion test small and deterministic.
    profile = get_profile()
    profile.source_size_limits = {"*_stateFlow.log": 16}
    monkeypatch.setattr("worker.tasks.get_profile", lambda: profile)
    return settings, store


def approve(job_id, settings):
    with SessionLocal() as db:
        job = db.get(ImportJob, job_id)
        path = Path(job.source_path)
        assert job.status == "awaiting_operator_confirmation"
        apply_import_action(db, job_id, "confirm", settings=settings, actor="approved-operator")
        db.commit()
    return path


def submit(batch, *, include_time=True):
    settings, _ = batch
    uploads = [LogUpload(MAIN, BytesIO(MAIN_BYTES))]
    if include_time:
        uploads.append(LogUpload(TIMING, BytesIO(TIMING_BYTES)))
    with SessionLocal() as db:
        result = upload_log_batch(PrintsRepository(db), uploads, settings=settings)
    job_id = result["jobs"][0]["import_job_id"]
    return job_id, approve(job_id, settings)


def submit_folder(batch, *, zipped=False, extras=()):
    settings, store = batch
    raw = Path(settings.raw_logs_container_path)
    (raw / MAIN).write_bytes(MAIN_BYTES)
    if zipped:
        with zipfile.ZipFile(raw / CONTAINER, "w") as archive:
            archive.writestr(f"native/path/{TIMING}", TIMING_BYTES)
    else:
        (raw / TIMING).write_bytes(TIMING_BYTES)
    for name in extras:
        (raw / name).write_bytes(b"excluded source larger than 16 bytes")
    store.expected_names = {MAIN, CONTAINER if zipped else TIMING, *extras}
    result = register_import_candidates([raw], settings=settings)[0]
    assert set(result.job.file_snapshot) == store.expected_names
    return result.job.import_job_id, approve(result.job.import_job_id, settings)


def load(job_id):
    with SessionLocal() as db:
        return db.get(ImportJob, job_id)


def next_attempt():
    time.sleep(1.05)
    return process_due_import_jobs("one-shot-native-policy-audit")


def assert_complete(job_id, batch, *, rows=2):
    settings, _ = batch
    job = load(job_id)
    assert job.status == "done", job.error
    assert job.owner_node_id == settings.compute_node_id
    assert job.confirmed_by == "approved-operator"
    assert job.lease_owner is None
    with SessionLocal() as db:
        sources = db.scalars(select(SourceFile)).all()
        assert {row.file_name for row in sources} == {MAIN, TIMING}
        assert len(db.scalars(select(LayerSnapshot)).all()) == rows
        session = db.get(BuildSession, job.session_ids[0])
        manifest = session.context["timing_publication"]
        assert manifest["status"] == "complete"
        assert manifest["source"]["import_job_id"] == job_id
        assert manifest["source"]["lease_generation"] == job.lease_generation
        assert db.get(ReportArtifact, job.report_ids[0]).payload["timing_publication"] == manifest
    return job


def after_source_checkpoint(monkeypatch, callback):
    from domain.services import import_jobs

    checkpoint = import_jobs.checkpoint_import_sources
    called = False

    def checkpoint_then_interrupt(fence, snapshot):
        nonlocal called
        checkpoint(fence, snapshot)
        if not called:
            called = True
            callback()

    monkeypatch.setattr(import_jobs, "checkpoint_import_sources", checkpoint_then_interrupt)


@pytest.mark.parametrize("lost_name", [MAIN, TIMING, CONTAINER])
def test_native_retries_never_legalize_missing_accepted_input(batch, tmp_path, monkeypatch, lost_name):
    settings, store = batch
    if lost_name == CONTAINER:
        job_id, path = submit_folder(batch, zipped=True)
        physical_names = {MAIN, CONTAINER}
    else:
        job_id, path = submit(batch)
        physical_names = {MAIN, TIMING}
    preserved = tmp_path / "preserved-accepted-source"
    expected_bytes = (path / lost_name).read_bytes()
    after_source_checkpoint(monkeypatch, lambda: (path / lost_name).rename(preserved))
    process_due_import_jobs("one-shot-native-policy-audit")
    first = load(job_id)
    assert first.status == "postponed"
    assert first.stability_check_attempts == first.execution_attempts == 0
    assert physical_names <= set(first.file_snapshot)
    assert store.objects == {}  # The accepted original is unavailable before capture/NAS IO.
    states = [first.status]
    for _ in range(settings.file_stability_max_retries + 1):
        next_attempt()
        job = load(job_id)
        states.append(job.status)
        if job.status in {"done", "failed"}:
            break
    assert "done" not in states, f"accepted {lost_name} vanished but retries completed: {states}"
    assert job.status == "failed"
    assert job.stability_check_attempts == settings.file_stability_max_retries
    assert job.execution_attempts == 0
    assert physical_names <= set(job.file_snapshot)
    assert job.owner_node_id == settings.compute_node_id
    assert job.confirmed_by == "approved-operator"
    assert job.lease_owner is None
    with SessionLocal() as db:
        for model in (BuildSession, ReportArtifact, SourceFile, LayerSnapshot):
            assert db.scalars(select(model)).all() == []
    assert preserved.read_bytes() == expected_bytes


@pytest.mark.parametrize("scenario", ["full", "restored", "addition", "append"])
def test_native_retry_restoration_and_growing_sources_are_allowed(batch, tmp_path, monkeypatch, scenario):
    job_id, path = submit(batch, include_time=scenario != "addition")
    if scenario == "restored":
        preserved = tmp_path / "preserved-time.log"
        after_source_checkpoint(monkeypatch, lambda: (path / TIMING).rename(preserved))
    elif scenario == "addition":
        (path / TIMING).write_bytes(TIMING_BYTES)
    elif scenario == "append":
        with (path / TIMING).open("ab") as output:
            output.write(b"OLD_STATS: 3|10000|31000|41300|\n")
    process_due_import_jobs("one-shot-native-policy-audit")
    if scenario != "full":
        assert load(job_id).status == "postponed"
        if scenario == "restored":
            preserved.rename(path / TIMING)
        next_attempt()
    assert_complete(job_id, batch, rows=3 if scenario == "append" else 2)
    assert (path / TIMING).read_bytes().startswith(TIMING_BYTES)


@pytest.mark.parametrize("lost_name", [MAIN, TIMING, CONTAINER])
def test_native_original_disappearing_after_archive_uses_full_captured_bytes(batch, tmp_path, lost_name):
    _, store = batch
    if lost_name == CONTAINER:
        job_id, path = submit_folder(batch, zipped=True)
    else:
        job_id, path = submit(batch)
    preserved = tmp_path / "preserved-original-source"
    expected_bytes = (path / lost_name).read_bytes()
    store.after_archive = lambda: (path / lost_name).rename(preserved)
    process_due_import_jobs("one-shot-native-policy-audit")
    job = assert_complete(job_id, batch)
    assert store.called and not (path / lost_name).exists()
    assert preserved.read_bytes() == expected_bytes
    assert lost_name in job.file_snapshot
    assert store.expected_names <= store.archive_names
    with SessionLocal() as db:
        sources = {row.file_name: row for row in db.scalars(select(SourceFile))}
        assert sources[MAIN].checksum == sha256(MAIN_BYTES).hexdigest()
        assert sources[TIMING].checksum == sha256(TIMING_BYTES).hexdigest()
        if lost_name != CONTAINER:
            assert sources[MAIN].original_path == str(path / MAIN)
            assert sources[TIMING].original_path == str(path / TIMING)


@pytest.mark.parametrize("excluded_name", [
    ".DS_Store", "01.04.2026_stateFlowData.log", "01.04.2026_stateFlow.log",
])
def test_native_missing_policy_excluded_source_still_completes(batch, tmp_path, excluded_name):
    job_id, path = submit_folder(batch, extras=[excluded_name])
    (path / excluded_name).rename(tmp_path / "preserved-excluded-source")
    process_due_import_jobs("one-shot-native-policy-audit")
    if load(job_id).status == "postponed":
        next_attempt()
    assert_complete(job_id, batch)


def test_native_broad_raw_snapshot_and_archival_are_preserved(batch):
    _, store = batch
    extras = {".DS_Store", "01.04.2026_stateFlowData.log", "01.04.2026_stateFlow.log"}
    job_id, _ = submit_folder(batch, extras=extras)
    before = load(job_id)
    assert set(before.file_snapshot) == {MAIN, TIMING, *extras}
    process_due_import_jobs("one-shot-native-policy-audit")
    job = assert_complete(job_id, batch)
    assert set(job.file_snapshot) == set(job.checksum_manifest) == {MAIN, TIMING, *extras}
    assert store.archive_names == {MAIN, TIMING, *extras}


def test_native_folder_zip_keeps_physical_snapshot_and_logical_parse_manifest(batch):
    _, store = batch
    job_id, _ = submit_folder(batch, zipped=True, extras=[".DS_Store"])
    process_due_import_jobs("one-shot-native-policy-audit")
    job = assert_complete(job_id, batch)
    assert set(job.file_snapshot) == {MAIN, CONTAINER, ".DS_Store"}
    assert set(job.checksum_manifest) == {MAIN, CONTAINER, ".DS_Store"}
    assert store.archive_names == {MAIN, CONTAINER, ".DS_Store"}
    with SessionLocal() as db:
        sources = {row.file_name: row for row in db.scalars(select(SourceFile))}
        assert sources[TIMING].object_uri.endswith(f"/{CONTAINER}")
        assert sources[TIMING].metadata_json["archive_member_path"] == f"native/path/{TIMING}"


@pytest.mark.parametrize("size,excluded", [(None, False), (True, False), (17.0, False),
                                         ("17", False), (-1, False), (16, False), (17, True)])
def test_pure_policy_requires_known_size_above_current_profile_limit(size, excluded):
    from domain.services.ingestion import source_exclusion_reason
    profile = get_profile()
    profile.source_size_limits = {"*_stateFlow.log": 16}
    assert bool(source_exclusion_reason("01.04.2026_stateFlow.log", size_bytes=size,
                                       profile=profile)) is excluded
