"""Bounded, integrity-checked object downloads for local estimator workers."""

import hashlib
import io

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
