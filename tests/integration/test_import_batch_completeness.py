from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.config.settings import get_settings
from domain.enums.common import ImportJobStatus, SourceFileFamily
from domain.models.sessions import BuildSession, SourceFile
from domain.services.import_jobs import confirm_import_job, detect_import_candidate, RetryableImportError
from domain.services.importing.fence import ImportFence
from domain.services.ingestion import IngestionService
from profiles.m350.profile import build_registry, get_profile
from storage.db.session import SessionLocal
from storage.repositories.import_jobs import ImportJobsRepository


class VerifiedStore:
    def __init__(self, expected_count):
        self.settings = SimpleNamespace(minio_bucket_raw="raw-logs")
        self.objects = {}
        self.expected_count = expected_count
        self.after_archive = None
        self.hook_called = False

    def is_available(self):
        return True

    def put_file_verified(self, bucket, object_name, path, *, expected_sha256, expected_size):
        data = path.read_bytes()
        assert sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size
        self.objects[object_name] = data
        if len(self.objects) == self.expected_count and not self.hook_called:
            self.hook_called = True
            if self.after_archive is not None:
                self.after_archive()
        return f"s3://{bucket}/{object_name}"


@pytest.fixture
def batch_attempt(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "compute_node_id", "completeness-node")
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "27.04.2026.log").write_text(
        "2026-04-27 10:00:00 start\n2026-04-27 10:00:01 finish\n", encoding="utf-8",
    )
    (folder / "27.04.2026_time.log").write_text("OLD_STATS: 1|9000|30000|39300|\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    job = detect_import_candidate(folder, settings=settings, now=now).job
    job.status = ImportJobStatus.checking_stability
    job.lease_owner = "completeness-worker"
    job.lease_generation = 1
    job.lease_until = now + timedelta(hours=1)
    with SessionLocal() as db:
        ImportJobsRepository(db).save_import_job(job)
        db.commit()
    store = VerifiedStore(2)
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", lambda: store)
    fence = ImportFence(job.import_job_id, job.owner_node_id, job.lease_owner, job.lease_generation)

    def guard():
        with SessionLocal() as db:
            fence.verify(db)
        return True

    return folder, job, settings, now, store, fence, guard


def run_attempt(attempt, registry=None, profile=None):
    _, job, settings, _, _, _, guard = attempt
    return confirm_import_job(job, registry or build_registry(), profile or get_profile(),
                              settings=settings, now=datetime.now(timezone.utc), lease_guard=guard)


def assert_no_partial_staging(attempt):
    _, job, _, _, _, fence, _ = attempt
    with SessionLocal() as db:
        assert db.scalars(select(BuildSession)).all() == []
        assert db.scalars(select(SourceFile)).all() == []
        saved = fence.verify(db)
        assert saved.owner_node_id == job.owner_node_id
        assert saved.status == "checking_stability"


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


def test_checkpointed_expected_file_disappearing_before_capture_cannot_finish_partial_import(batch_attempt, tmp_path, monkeypatch):
    folder, job, _, _, store, _, _ = batch_attempt
    missing = folder / "27.04.2026_time.log"
    after_source_checkpoint(monkeypatch, lambda: missing.rename(tmp_path / "preserved-time.log"))
    with pytest.raises(RetryableImportError, match="непол|отсутств|недоступ"):
        run_attempt(batch_attempt)
    assert job.checksum_manifest == store.objects == {}
    assert (tmp_path / "preserved-time.log").read_text().startswith("OLD_STATS")
    with SessionLocal() as db:
        assert set(batch_attempt[5].verify(db).file_snapshot) == {"27.04.2026.log", "27.04.2026_time.log"}
    assert_no_partial_staging(batch_attempt)


def test_archived_staged_file_read_failure_cannot_finish_partial_import(batch_attempt, monkeypatch):
    folder, job, _, _, store, _, _ = batch_attempt
    from domain.services.ingestion import sha256_file as real_sha
    denied_paths = []

    def denied(path):
        if store.hook_called and path.name == "27.04.2026_time.log":
            assert path != folder / "27.04.2026_time.log"
            denied_paths.append(path)
            raise PermissionError("captured source read denied after archive")
        return real_sha(path)
    monkeypatch.setattr("domain.services.ingestion.sha256_file", denied)
    with pytest.raises(RetryableImportError, match="непол|отсутств|недоступ"):
        run_attempt(batch_attempt)
    assert len(job.checksum_manifest) == len(store.objects) == 2
    assert len(denied_paths) == 1
    assert (folder / "27.04.2026_time.log").read_text().startswith("OLD_STATS")
    assert_no_partial_staging(batch_attempt)


def test_archived_staged_file_missing_before_ingestion_cannot_finish_partial_import(batch_attempt, tmp_path, monkeypatch):
    folder, job, _, _, store, _, _ = batch_attempt
    parse = IngestionService.parse
    preserved = tmp_path / "preserved-captured-time.log"
    original = folder / "27.04.2026_time.log"
    original_bytes = original.read_bytes()

    def drop_captured_source(ingestion, work_root):
        captured = work_root / original.name
        assert captured != original and store.hook_called
        captured.rename(preserved)
        return parse(ingestion, work_root)

    monkeypatch.setattr(IngestionService, "parse", drop_captured_source)
    with pytest.raises(RetryableImportError, match="непол|отсутств|недоступ"):
        run_attempt(batch_attempt)
    assert len(job.checksum_manifest) == len(store.objects) == 2
    assert preserved.read_bytes() == original.read_bytes() == original_bytes
    assert_no_partial_staging(batch_attempt)


def test_original_file_disappearing_after_archive_keeps_full_captured_import(batch_attempt, tmp_path):
    folder, job, _, _, store, _, _ = batch_attempt
    original = folder / "27.04.2026_time.log"
    preserved = tmp_path / "preserved-original-time.log"
    expected_bytes = original.read_bytes()
    store.after_archive = lambda: original.rename(preserved)
    result = run_attempt(batch_attempt)
    assert result.job.status == ImportJobStatus.done
    raw_objects = {name: data for name, data in store.objects.items() if name.startswith("imports/")}
    assert len(job.checksum_manifest) == len(raw_objects) == 2
    assert {sha256(data).hexdigest() for data in raw_objects.values()} == set(job.checksum_manifest.values())
    assert not original.exists() and preserved.read_bytes() == expected_bytes
    with SessionLocal() as db:
        sources = db.scalars(select(SourceFile)).all()
        assert len(sources) == 2
        assert {source.checksum for source in sources} == set(job.checksum_manifest.values())
        timing = next(source for source in sources if source.file_name == original.name)
        assert timing.original_path == str(original)
        assert timing.checksum == sha256(expected_bytes).hexdigest()


def test_scan_read_error_has_a_distinct_structured_category(tmp_path, monkeypatch):
    path = tmp_path / "job.log"
    path.write_text("input", encoding="utf-8")
    monkeypatch.setattr("domain.services.ingestion.sha256_file", lambda path:
                        (_ for _ in ()).throw(PermissionError("denied")))
    result = IngestionService(build_registry(), get_profile()).scan(path)
    assert result.skipped[0]["code"] == "source_inspection_error"
    assert result.skipped[0]["path"] == str(path)


@pytest.mark.parametrize("name", [".DS_Store", "27.04.2026_stateFlowData.log"])
def test_name_exclusions_do_not_require_a_second_stat(tmp_path, monkeypatch, name):
    from pathlib import Path

    path = tmp_path / name
    path.write_bytes(b"intentionally excluded source")
    original_stat = Path.stat
    calls = 0

    def race_after_is_file(candidate, *args, **kwargs):
        nonlocal calls
        if candidate == path:
            calls += 1
            if calls > 1:
                raise FileNotFoundError("source rotated after is_file")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", race_after_is_file)
    result = IngestionService(build_registry(), get_profile()).scan(tmp_path)
    assert result.files == []
    assert len(result.skipped) == 1
    assert result.skipped[0]["code"] == "source_policy_excluded"
    assert calls == 1


def test_registry_caught_parser_exception_is_processing_failure(batch_attempt, monkeypatch):
    registry = build_registry()

    def broken(*args, **kwargs):
        raise RuntimeError("parser state broken")
    monkeypatch.setattr(registry.get(SourceFileFamily.main_event_log), "parse", broken)
    result = run_attempt(batch_attempt, registry=registry)
    assert result.job.status == ImportJobStatus.failed
    assert "parser" in result.job.error.lower()
    assert_no_partial_staging(batch_attempt)


def test_ingestion_caught_parser_failure_is_processing_failure(batch_attempt, monkeypatch):
    registry = build_registry()
    original = registry.parse

    def broken(path, family, context):
        if family == SourceFileFamily.main_event_log:
            raise RuntimeError("registry processing failed")
        return original(path, family, context)
    monkeypatch.setattr(registry, "parse", broken)
    result = run_attempt(batch_attempt, registry=registry)
    assert result.job.status == ImportJobStatus.failed
    assert_no_partial_staging(batch_attempt)


def test_explicit_policy_skips_do_not_make_batch_incomplete(batch_attempt):
    folder, job, _, _, store, _, _ = batch_attempt
    extras = {".DS_Store": b"metadata", "27.04.2026_stateFlowData.log": b"unsupported native data",
              "27.04.2026_stateFlow.log": b"large stateflow for configured size gate"}
    for name, data in extras.items():
        (folder / name).write_bytes(data)
    from domain.services.import_jobs import snapshot_source
    job.file_snapshot = snapshot_source(folder)
    store.expected_count += len(extras)
    profile = get_profile()
    profile.source_size_limits = {"*_stateFlow.log": 16}
    result = run_attempt(batch_attempt, profile=profile)
    assert result.job.status == ImportJobStatus.done
    assert len(job.checksum_manifest) == 5
    assert result.job.session_ids


def test_disappeared_metadata_still_matches_intentional_exclusion(batch_attempt, tmp_path):
    folder, job, _, _, store, _, _ = batch_attempt
    metadata = folder / ".DS_Store"
    metadata.write_bytes(b"metadata")
    from domain.services.import_jobs import snapshot_source
    job.file_snapshot = snapshot_source(folder)
    store.expected_count = 3
    store.after_archive = lambda: metadata.rename(tmp_path / "preserved-metadata")
    assert run_attempt(batch_attempt).job.status == ImportJobStatus.done


def test_intentional_unsupported_or_large_parser_result_is_not_exception(batch_attempt, monkeypatch):
    folder, job, _, _, store, _, _ = batch_attempt
    (folder / "unknown.bin").write_bytes(b"\x00\x01")
    (folder / "27.04.2026_stateFlow.log").write_text("Time|State|\n12:00:00|1|\n")
    from domain.services.import_jobs import snapshot_source
    job.file_snapshot = snapshot_source(folder)
    store.expected_count = 4
    monkeypatch.setattr("parsers.formats.stateflow_log.StateFlowLogParser.MAX_FILE_SIZE_BYTES", 1)
    result = run_attempt(batch_attempt)
    assert result.job.status == ImportJobStatus.done


def test_malformed_content_retains_input_without_becoming_parser_failure(batch_attempt):
    folder, _, _, _, _, _, _ = batch_attempt
    (folder / "27.04.2026_time.log").write_text("OLD_STATS: bad|row|\n", encoding="utf-8")
    from domain.services.import_jobs import snapshot_source
    batch_attempt[1].file_snapshot = snapshot_source(folder)
    result = run_attempt(batch_attempt)
    assert result.job.status == ImportJobStatus.done
    with SessionLocal() as db:
        assert len(db.scalars(select(SourceFile)).all()) == 2


def test_restored_source_completes_one_full_fenced_attempt(batch_attempt, tmp_path, monkeypatch):
    folder, _, _, _, _, _, _ = batch_attempt
    original = folder / "27.04.2026_time.log"
    parked = tmp_path / "preserved-time.log"
    after_source_checkpoint(monkeypatch, lambda: original.rename(parked))
    with pytest.raises(RetryableImportError):
        run_attempt(batch_attempt)
    assert_no_partial_staging(batch_attempt)
    parked.rename(original)
    result = run_attempt(batch_attempt)
    assert result.job.status == ImportJobStatus.done
    with SessionLocal() as db:
        sources = db.scalars(select(SourceFile)).all()
        assert len(sources) == 2
        assert {source.checksum for source in sources} == set(result.job.checksum_manifest.values())
        session = db.get(BuildSession, result.job.session_ids[0])
        assert session.origin_compute_node_id == result.job.owner_node_id
