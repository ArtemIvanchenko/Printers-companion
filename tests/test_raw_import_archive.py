from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.config.settings import Settings
from domain.enums.common import ImportJobStatus
from domain.services.import_jobs import (
    ImportJobRecord,
    RawArchiveUnavailableError,
    archive_raw_import,
    confirm_import_job,
    mark_import_job_confirmed,
    snapshot_source,
)
from domain.services.importing.fence import ImportFence
from domain.services.importing.publication import publish_import
from profiles.m350.profile import build_registry, get_profile
from storage.db.session import SessionLocal
from storage.repositories.import_jobs import ImportJobsRepository


class _Store:
    def __init__(self, available: bool = True):
        self.available = available
        self.objects: dict[str, bytes] = {}

        class _Settings:
            minio_bucket_raw = "raw-logs"

        self.settings = _Settings()

    def is_available(self) -> bool:
        return self.available

    def put_file_verified(
        self,
        bucket: str,
        object_name: str,
        path: Path,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> str:
        assert bucket == "raw-logs"
        payload = path.read_bytes()
        assert len(payload) == expected_size
        import hashlib

        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        self.objects.setdefault(object_name, payload)
        return f"s3://{bucket}/{object_name}"


def _job(source: Path, *, kind: str = "folder") -> ImportJobRecord:
    return ImportJobRecord(
        import_job_id="import_test",
        owner_node_id="operator-01",
        source_path=str(source),
        source_name=source.name,
        source_kind=kind,
    )


def test_complete_folder_is_archived_before_analysis(tmp_path, monkeypatch):
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "main.log").write_bytes(b"main")
    nested = batch / "nested"
    nested.mkdir()
    (nested / "time.log").write_bytes(b"time")
    store = _Store()
    monkeypatch.setattr(
        "storage.object_store.minio_client.ObjectStore",
        lambda: store,
    )

    objects = archive_raw_import(_job(batch), batch)

    assert set(objects) == {"main.log", "nested/time.log"}
    time_key = next(
        key for key in store.objects
        if key.endswith("/time.log")
    )
    assert "/files/" in time_key
    assert store.objects[time_key] == b"time"


def test_nas_unavailable_blocks_a_required_archive(tmp_path, monkeypatch):
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "main.log").write_bytes(b"main")
    monkeypatch.setattr(
        "storage.object_store.minio_client.ObjectStore",
        lambda: _Store(available=False),
    )

    with pytest.raises(RuntimeError, match="NAS object storage is unavailable"):
        archive_raw_import(_job(batch), batch, required=True)


def test_nas_archive_outage_is_retried_without_terminal_limit(tmp_path, monkeypatch):
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "main.log").write_bytes(b"main")
    now = datetime.now(timezone.utc)
    job = _job(batch)
    job.stability_check_attempts = 50
    job.lease_generation = 51

    def _unavailable(*args, **kwargs):
        raise RawArchiveUnavailableError("NAS unavailable")

    monkeypatch.setattr(
        "domain.services.import_jobs.execute_confirmed_import",
        _unavailable,
    )
    settings = Settings(
        file_stability_seconds=0,
        nas_sync_retry_min_seconds=5,
        nas_sync_retry_max_seconds=300,
    )

    result = confirm_import_job(
        job,
        registry=object(),
        settings=settings,
        now=now,
    )

    assert result.job.status == ImportJobStatus.postponed
    assert result.job.stability_check_attempts == 50
    assert result.job.postponed_until is not None
    assert (result.job.postponed_until - now).total_seconds() == 300
    assert result.job.audit_trail[-1]["action"] == "raw_archive_deferred"
    assert result.job.audit_trail[-1]["details"]["attempt"] == 51


def test_repeated_nas_outage_does_not_exhaust_changed_file_stability_budget(
    tmp_path, monkeypatch
):
    batch = tmp_path / "batch"
    batch.mkdir()
    log = batch / "job.log"
    log.write_text("2026-04-27 10:00:00 Старт печати\n", encoding="utf-8")
    job = _job(batch)
    settings = Settings(
        file_stability_seconds=0,
        file_stability_max_retries=2,
        nas_sync_retry_min_seconds=5,
        nas_sync_retry_max_seconds=20,
    )
    registry, profile = build_registry(), get_profile()
    now = datetime.now(timezone.utc)
    job.file_snapshot = snapshot_source(batch)
    mark_import_job_confirmed(job, now=now)
    with SessionLocal() as db:
        ImportJobsRepository(db).save_import_job(job)
        db.commit()

    def run_claimed_attempt(at):
        with SessionLocal() as db:
            claimed = ImportJobsRepository(db).claim_next_import_job(
                owner_node_id=job.owner_node_id, lease_owner="raw-archive-test-worker",
                now=at, lease_seconds=3600,
            )
            db.commit()
        assert claimed is not None
        fence = ImportFence(claimed.import_job_id, claimed.owner_node_id,
                            claimed.lease_owner, claimed.lease_generation)
        result = confirm_import_job(
            claimed, registry=registry, profile=profile, settings=settings, now=at,
        )
        if result.job.status != ImportJobStatus.done:
            with SessionLocal() as db:
                publish_import(db, result, fence=fence, prepared_reports={})
                db.commit()
        return result

    def unavailable(*args, **kwargs):
        raise RawArchiveUnavailableError("NAS unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("domain.services.import_jobs.archive_raw_import", unavailable)
        for generation in range(1, 5):
            result = run_claimed_attempt(now)
            job = result.job
            assert job.lease_generation == generation
            assert result.job.status == ImportJobStatus.postponed
            assert result.job.stability_check_attempts == 0
            assert result.job.postponed_until > now
            now = result.job.postponed_until

    # A real source change after the outage gets its own bounded stability retry.
    log.write_text(
        "2026-04-27 10:00:00 Старт печати\n2026-04-27 10:01:00 слой 1\n", encoding="utf-8"
    )
    result = run_claimed_attempt(now)
    assert result.job.status == ImportJobStatus.postponed
    assert result.job.stability_check_attempts == 1

    result = run_claimed_attempt(result.job.postponed_until)
    assert result.job.status == ImportJobStatus.done
    assert result.job.session_ids
    assert result.job.report_ids
