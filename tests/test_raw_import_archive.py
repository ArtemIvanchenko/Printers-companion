from pathlib import Path

import pytest

from domain.services.import_jobs import ImportJobRecord, archive_raw_import


class _Store:
    def __init__(self, available: bool = True):
        self.available = available
        self.objects: dict[str, bytes] = {}

        class _Settings:
            minio_bucket_raw = "raw-logs"

        self.settings = _Settings()

    def is_available(self) -> bool:
        return self.available

    def put_file(self, bucket: str, object_name: str, path: Path) -> str:
        assert bucket == "raw-logs"
        self.objects[object_name] = path.read_bytes()
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
