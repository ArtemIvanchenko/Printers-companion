"""Bounded, integrity-checked object downloads for local estimator workers."""

import hashlib
import io
from pathlib import Path

import pytest
from minio.error import S3Error

from storage.object_store.minio_client import ObjectStore


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.stream = io.BytesIO(payload)
        self.read_sizes: list[int] = []
        self.closed = False
        self.released = False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def get_object(self, bucket: str, object_name: str) -> _Response:
        assert bucket == "stls"
        assert object_name == "plate/model.stl"
        return self.response


def _store(response: _Response) -> ObjectStore:
    store = ObjectStore.__new__(ObjectStore)
    store.client = _Client(response)
    return store


def test_download_file_streams_valid_object_and_releases_connection(tmp_path):
    payload = b"mesh-data-" * 1000
    response = _Response(payload)
    destination = tmp_path / "model.stl"

    result = _store(response).download_file(
        "stls",
        "plate/model.stl",
        destination,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        chunk_size=97,
    )

    assert result == destination
    assert destination.read_bytes() == payload
    assert response.read_sizes and set(response.read_sizes) == {97}
    assert response.closed and response.released
    assert list(tmp_path.glob("*.part")) == []


def test_download_file_rejects_checksum_mismatch_without_partial_file(tmp_path):
    response = _Response(b"corrupted")
    destination = tmp_path / "model.stl"

    result = _store(response).download_file(
        "stls",
        "plate/model.stl",
        destination,
        expected_sha256="0" * 64,
    )

    assert result is None
    assert not destination.exists()
    assert response.closed and response.released
    assert list(tmp_path.iterdir()) == []


class _Stat:
    def __init__(self, data: bytes, sha256: str) -> None:
        self.size = len(data)
        self.metadata = {"x-amz-meta-sha256": sha256}


class _AtomicClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.put_count = 0

    def stat_object(self, bucket: str, object_name: str) -> _Stat:
        try:
            data, checksum = self.objects[(bucket, object_name)]
        except KeyError:
            raise S3Error(
                None, "NoSuchKey", "missing", object_name, "request", "host", bucket,
                object_name,
            ) from None
        return _Stat(data, checksum)

    def fput_object(self, bucket, object_name, path, *, content_type, metadata):
        self.put_count += 1
        self.objects[(bucket, object_name)] = (
            Path(path).read_bytes(),
            metadata["sha256"],
        )


def _atomic_store(client: _AtomicClient) -> ObjectStore:
    store = ObjectStore.__new__(ObjectStore)
    store.client = client
    store.ensure_bucket = lambda bucket: None
    return store


def test_verified_put_is_atomic_and_idempotent(tmp_path):
    payload = b"verified-model"
    path = tmp_path / "model.stl"
    path.write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    client = _AtomicClient()
    store = _atomic_store(client)

    first = store.put_file_verified(
        "stls", "record/model", path,
        expected_sha256=checksum, expected_size=len(payload),
    )
    second = store.put_file_verified(
        "stls", "record/model", path,
        expected_sha256=checksum, expected_size=len(payload),
    )

    assert first == second == "s3://stls/record/model"
    assert client.put_count == 1


def test_verified_put_fails_closed_on_immutable_key_conflict(tmp_path):
    payload = b"expected"
    path = tmp_path / "model.stl"
    path.write_bytes(payload)
    client = _AtomicClient()
    client.objects[("stls", "record/model")] = (b"unexpected", "0" * 64)

    with pytest.raises(RuntimeError, match="conflicts"):
        _atomic_store(client).put_file_verified(
            "stls", "record/model", path,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload),
        )


def test_verified_put_rejects_local_checksum_mismatch_before_nas_write(tmp_path):
    path = tmp_path / "model.stl"
    path.write_bytes(b"changed")
    client = _AtomicClient()

    with pytest.raises(ValueError, match="checksum"):
        _atomic_store(client).put_file_verified(
            "stls",
            "record/model",
            path,
            expected_sha256=hashlib.sha256(b"original").hexdigest(),
            expected_size=len(b"changed"),
        )

    assert client.put_count == 0
