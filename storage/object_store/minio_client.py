import hashlib
import io
import os
import tempfile
from pathlib import Path

from minio import Minio
from minio.error import S3Error

from core.config.settings import Settings, get_settings
from core.utils.files import sha256_file


class ObjectStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = Minio(
            self.settings.minio_endpoint,
            access_key=self.settings.minio_root_user,
            secret_key=self.settings.minio_root_password,
            secure=self.settings.minio_secure,
        )

    def ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def ensure_all_buckets(self) -> None:
        """Create every bucket the application uses (idempotent, best-effort)."""
        for bucket in (
            self.settings.minio_bucket_raw,
            self.settings.minio_bucket_reports,
            self.settings.minio_bucket_stls,
            self.settings.minio_bucket_magics,
            self.settings.minio_bucket_photos,
            self.settings.minio_bucket_docs,
        ):
            self.ensure_bucket(bucket)

    def put_file(
        self,
        bucket: str,
        object_name: str,
        path: Path,
        content_type: str = "application/octet-stream",
    ) -> str:
        self.ensure_bucket(bucket)
        self.client.fput_object(bucket, object_name, str(path), content_type=content_type)
        return f"s3://{bucket}/{object_name}"

    @staticmethod
    def _stat_sha256(stat: object) -> str | None:
        metadata = getattr(stat, "metadata", None) or {}
        normalized = {str(key).lower(): str(value).lower() for key, value in metadata.items()}
        return normalized.get("x-amz-meta-sha256") or normalized.get("sha256")

    def put_file_verified(
        self,
        bucket: str,
        object_name: str,
        path: Path,
        *,
        expected_sha256: str,
        expected_size: int,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Atomically publish a checksum-addressed file and verify the result.

        MinIO exposes a completed PUT atomically; multipart fragments are not
        visible at ``object_name``.  The SHA-256 metadata and size check make a
        process/network retry safe: an already published identical object is a
        success, while an unexpected object at the immutable key fails closed.
        """
        path = Path(path)
        expected_sha256 = expected_sha256.lower()
        if path.stat().st_size != int(expected_size):
            raise ValueError("Object-store upload size does not match its manifest")
        if sha256_file(path).lower() != expected_sha256:
            raise ValueError("Object-store upload checksum does not match its manifest")
        self.ensure_bucket(bucket)
        try:
            current = self.client.stat_object(bucket, object_name)
        except S3Error as exc:
            if exc.code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                raise
            current = None

        if current is not None:
            if (
                int(getattr(current, "size", -1)) == int(expected_size)
                and self._stat_sha256(current) == expected_sha256
            ):
                return f"s3://{bucket}/{object_name}"
            raise RuntimeError("Immutable MinIO object conflicts with upload manifest")

        self.client.fput_object(
            bucket,
            object_name,
            str(path),
            content_type=content_type,
            metadata={"sha256": expected_sha256},
        )
        published = self.client.stat_object(bucket, object_name)
        if (
            int(getattr(published, "size", -1)) != int(expected_size)
            or self._stat_sha256(published) != expected_sha256
        ):
            raise RuntimeError("MinIO did not verify the published object's checksum")
        return f"s3://{bucket}/{object_name}"

    def put_bytes(
        self, bucket: str, object_name: str, data: bytes,
        content_type: str = "application/json",
    ) -> str:
        """Upload an in-memory blob; returns its s3://bucket/object URI."""
        self.ensure_bucket(bucket)
        self.client.put_object(
            bucket, object_name, io.BytesIO(data), length=len(data), content_type=content_type,
        )
        return f"s3://{bucket}/{object_name}"

    def get_bytes(self, bucket: str, object_name: str) -> bytes | None:
        """Download an object's bytes, or None if missing/unavailable."""
        try:
            response = self.client.get_object(bucket, object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception:
            return None

    def download_file(
        self,
        bucket: str,
        object_name: str,
        destination: Path,
        *,
        expected_sha256: str | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> Path | None:
        """Stream an object into a local file without buffering it in memory.

        The download is written to a sibling temporary file and atomically
        moved into place only after the complete response (and, when supplied,
        its SHA-256 checksum) has been validated. ``None`` means that the
        transfer failed or the checksum did not match. Partial files are
        always removed.
        """
        destination = Path(destination)
        response = None
        temporary_path: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            response = self.client.get_object(bucket, object_name)
            digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".part",
                delete=False,
            ) as sink:
                temporary_path = Path(sink.name)
                while chunk := response.read(chunk_size):
                    sink.write(chunk)
                    digest.update(chunk)

            if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
                temporary_path.unlink(missing_ok=True)
                return None
            os.replace(temporary_path, destination)
            temporary_path = None
            return destination
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            return None
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def open_stream(self, bucket: str, object_name: str, chunk_size: int = 1024 * 1024):
        """Yield an object's bytes in chunks, or None if missing/unavailable.

        For anything that can be large (STLs are capped at 600 MB), use this
        instead of get_bytes(): the whole object never sits in memory, and the
        connection is released even if the client disconnects mid-download.
        """
        try:
            response = self.client.get_object(bucket, object_name)
        except Exception:
            return None

        def _iterator():
            try:
                while chunk := response.read(chunk_size):
                    yield chunk
            finally:
                response.close()
                response.release_conn()

        return _iterator()

    def remove_object(self, bucket: str, object_name: str) -> bool:
        """Delete an object; True on success, False if missing/unavailable."""
        try:
            self.client.remove_object(bucket, object_name)
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        try:
            self.client.list_buckets()
            return True
        except S3Error:
            return False
        except Exception:
            return False
