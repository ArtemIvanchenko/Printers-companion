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
)


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
    assert result.job.stability_check_attempts == 51
    assert result.job.postponed_until is not None
    assert (result.job.postponed_until - now).total_seconds() == 300
    assert result.job.audit_trail[-1]["action"] == "raw_archive_deferred"
